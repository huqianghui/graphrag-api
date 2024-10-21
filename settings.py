import yaml
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    GRAPHRAG_LLM_API_URL: str
    GRAPHRAG_LLM_API_KEY: str
    GRAPHRAG_LLM_DEPLOYMENT: str
    GRAPHRAG_EMBEDDING_API_URL: str
    GRAPHRAG_LLM_API_VERSION: str
    GRAPHRAG_EMBEDDING_API_KEY: str
    GRAPHRAG_EMBEDDING_DEPLOYMENT: str
    GRAPHRAG_CLAIM_EXTRACTION_ENABLED: bool
    INPUT_DIR: str
    COMMUNITY_LEVEL: int

    class Config:
        env_file = ".env"
        

def load_settings_from_yaml(yaml_file: str) -> Settings:
    with open(yaml_file, 'r', encoding='utf-8') as file:
        config_dict = yaml.safe_load(file)
    return Settings(**config_dict)
