import os

import pandas as pd
import tiktoken
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from graphrag.query.context_builder.entity_extraction import EntityVectorStoreKey
from graphrag.query.indexer_adapters import (
    read_indexer_covariates,
    read_indexer_entities,
    read_indexer_relationships,
    read_indexer_reports,
    read_indexer_text_units,
)
from graphrag.query.input.loaders.dfs import store_entity_semantic_embeddings
from graphrag.query.llm.oai.chat_openai import ChatOpenAI
from graphrag.query.llm.oai.embedding import OpenAIEmbedding
from graphrag.query.llm.oai.typing import OpenaiApiType
from graphrag.query.structured_search.global_search.community_context import (
    GlobalCommunityContext,
)
from graphrag.query.structured_search.global_search.search import GlobalSearch
from graphrag.query.structured_search.local_search.mixed_context import (
    LocalSearchMixedContext,
)
from graphrag.query.structured_search.local_search.search import LocalSearch
from graphrag.vector_stores.lancedb import LanceDBVectorStore

from constants import (
    COMMUNITY_REPORT_TABLE,
    COVARIATE_TABLE,
    ENTITY_EMBEDDING_TABLE,
    ENTITY_TABLE,
    RELATIONSHIP_TABLE,
    TEXT_UNIT_TABLE,
)
from settings import load_settings_from_yaml
from utils import (
    convert_response_to_string,
    process_context_data,
    serialize_search_result,
)

_ = load_dotenv()

settings = load_settings_from_yaml("settings.yml")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Load environment variables
llm_url = settings.GRAPHRAG_LLM_API_KEY
llm_api_key = settings.GRAPHRAG_LLM_API_KEY
llm_deployment_name = settings.GRAPHRAG_LLM_DEPLOYMENT
llm_api_version = settings.GRAPHRAG_LLM_API_VERSION

embedding_url = settings.GRAPHRAG_EMBEDDING_API_URL
embedding_api_key = settings.GRAPHRAG_EMBEDDING_API_KEY
embedding_deployment_name = settings.GRAPHRAG_EMBEDDING_DEPLOYMENT
claim_extraction_enabled = settings.GRAPHRAG_CLAIM_EXTRACTION_ENABLED

llm = ChatOpenAI(
    api_base=llm_url,
    api_key=llm_api_key,
    api_version=llm_api_version,
    deployment_name=llm_deployment_name,
    api_type=OpenaiApiType.AzureOpenAI,
    max_retries=20,
)

token_encoder = tiktoken.get_encoding("o200k_base")

INPUT_DIR = settings.INPUT_DIR
COMMUNITY_LEVEL = settings.COMMUNITY_LEVEL

def load_parquet_files():
    entity_df = pd.read_parquet(f"{INPUT_DIR}/{ENTITY_TABLE}.parquet")
    entity_embedding_df = pd.read_parquet(f"{INPUT_DIR}/{ENTITY_EMBEDDING_TABLE}.parquet")
    report_df = pd.read_parquet(f"{INPUT_DIR}/{COMMUNITY_REPORT_TABLE}.parquet")
    relationship_df = pd.read_parquet(f"{INPUT_DIR}/{RELATIONSHIP_TABLE}.parquet")    
    covariate_df = pd.read_parquet(f"{INPUT_DIR}/{COVARIATE_TABLE}.parquet") if claim_extraction_enabled else pd.DataFrame()
    text_unit_df = pd.read_parquet(f"{INPUT_DIR}/{TEXT_UNIT_TABLE}.parquet")
    
    return entity_df, entity_embedding_df, report_df, relationship_df, covariate_df, text_unit_df

entity_df, entity_embedding_df, report_df, relationship_df, covariate_df, text_unit_df = load_parquet_files()

def setup_global_search():
    entities = read_indexer_entities(entity_df, entity_embedding_df, COMMUNITY_LEVEL)
    reports = read_indexer_reports(report_df, entity_df, COMMUNITY_LEVEL)
    
    context_builder = GlobalCommunityContext(
        community_reports=reports,
        entities=entities,
        token_encoder=token_encoder,
    )

    search_engine = GlobalSearch(
        llm=llm,
        context_builder=context_builder,
        token_encoder=token_encoder,
        max_data_tokens=12_000,
        map_llm_params={
            "max_tokens": 1000,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        },
        reduce_llm_params={
            "max_tokens": 2000,
            "temperature": 0.0,
        },
        allow_general_knowledge=False,
        json_mode=True,
        context_builder_params={
            "use_community_summary": False,
            "shuffle_data": True,
            "include_community_rank": True,
            "min_community_rank": 0,
            "community_rank_name": "rank",
            "include_community_weight": True,
            "community_weight_name": "occurrence weight",
            "normalize_community_weight": True,
            "max_tokens": 12_000,
            "context_name": "Reports",
        },
        concurrent_coroutines=32,
        response_type="single paragraph",
    )
    return search_engine

def setup_local_search():
    entities = read_indexer_entities(entity_df, entity_embedding_df, COMMUNITY_LEVEL)
    relationships = read_indexer_relationships(relationship_df)
    claims = read_indexer_covariates(covariate_df) if claim_extraction_enabled else []
    reports = read_indexer_reports(report_df, entity_df, COMMUNITY_LEVEL)
    text_units = read_indexer_text_units(text_unit_df)

    description_embedding_store = LanceDBVectorStore(
        collection_name="entity_description_embeddings",
    )
    description_embedding_store.connect(db_uri=f"{INPUT_DIR}/lancedb")
    store_entity_semantic_embeddings(entities=entities, vectorstore=description_embedding_store)

    context_builder = LocalSearchMixedContext(
        community_reports=reports,
        text_units=text_units,
        entities=entities,
        relationships=relationships,
        covariates={"claims": claims} if claim_extraction_enabled else None,
        entity_text_embeddings=description_embedding_store,
        embedding_vectorstore_key=EntityVectorStoreKey.ID,
        text_embedder=OpenAIEmbedding(
            api_base=embedding_url,
            api_key=embedding_api_key,
            api_type=OpenaiApiType.AzureOpenAI,
            api_version=llm_api_version,
            deployment_name=embedding_deployment_name,
            max_retries=20,
        ),
        token_encoder=token_encoder,
    )

    search_engine = LocalSearch(
        llm=llm,
        context_builder=context_builder,
        token_encoder=token_encoder,
        llm_params={
            "max_tokens": 2_000,
            "temperature": 0.0,
        },
        context_builder_params={
            "text_unit_prop": 0.5,
            "community_prop": 0.1,
            "conversation_history_max_turns": 5,
            "conversation_history_user_turns_only": True,
            "top_k_mapped_entities": 10,
            "top_k_relationships": 10,
            "include_entity_rank": True,
            "include_relationship_weight": True,
            "include_community_rank": False,
            "return_candidate_context": False,
            "embedding_vectorstore_key": EntityVectorStoreKey.ID,
            "max_tokens": 12_000,
        },
        response_type="single paragraph",
    )
    return search_engine

global_search_engine = setup_global_search()
local_search_engine = setup_local_search()

@app.get("/search/global")
async def global_search(query: str = Query(..., description="Search query for global context")):
    try:
        result = await global_search_engine.asearch(query)        
        response_dict = {
            "response": convert_response_to_string(result.response),
            "context_data": process_context_data(result.context_data),
            "context_text": result.context_text,
            "completion_time": result.completion_time,
            "llm_calls": result.llm_calls,
            "prompt_tokens": result.prompt_tokens,
            "reduce_context_data": process_context_data(result.reduce_context_data),
            "reduce_context_text": result.reduce_context_text,
            "map_responses": [serialize_search_result(result) for result in result.map_responses],
        }
        return JSONResponse(content=response_dict)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/search/local")
async def local_search(query: str = Query(..., description="Search query for local context")):
    try:
        result = await local_search_engine.asearch(query)        
        response_dict = {
            "response": convert_response_to_string(result.response),
            "context_data": process_context_data(result.context_data),
            "context_text": result.context_text,
            "completion_time": result.completion_time,
            "llm_calls": result.llm_calls,
            "prompt_tokens": result.prompt_tokens,            
        }
        return JSONResponse(content=response_dict)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def status():
    return JSONResponse(content={"status": "Server is up and running"})

STATIC_FOLDER = 'static/artifacts/'

@app.get("/parquet/{filename}")
async def download_file(filename: str):
    file_path = os.path.join(STATIC_FOLDER, filename)
    
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    return FileResponse(path=file_path, filename=filename, media_type='application/octet-stream')



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
