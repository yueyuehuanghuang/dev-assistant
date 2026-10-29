"""Optional localhost-only adapter for an existing SentenceTransformer model directory."""
from contextlib import asynccontextmanager
from functools import lru_cache
import os
from pathlib import Path
import threading
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

load_dotenv(Path(__file__).with_name('.env'))
os.environ.setdefault('HF_HUB_OFFLINE', '1')
os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')
inference_lock = threading.Lock()


@lru_cache(maxsize=1)
def local_model():
    path = Path(os.getenv('LOCAL_EMBEDDING_MODEL', '')).expanduser()
    if not path.is_dir() or not (path / 'modules.json').is_file():
        raise RuntimeError('LOCAL_EMBEDDING_MODEL must be an existing local SentenceTransformer directory')
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(str(path.resolve()), device=os.getenv('LOCAL_EMBEDDING_DEVICE', 'cpu'),
                                local_files_only=True, trust_remote_code=False)
    model.max_seq_length = 2048
    return model


@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(local_model)
    yield
    local_model.cache_clear()


app = FastAPI(title='Dev Assistant local embeddings', lifespan=lifespan)


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str = Field(max_length=200)
    input: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(min_length=1, max_length=16)
    dimensions: int | None = Field(default=None, ge=1, le=8192)


@app.get('/health')
def health():
    return {'status': 'ok', 'model': os.getenv('EMBEDDING_MODEL', 'Qwen/Qwen3-Embedding-0.6B'),
            'dimensions': local_model().get_sentence_embedding_dimension(), 'local_only': True}


@app.post('/v1/embeddings')
def embed(payload: EmbeddingRequest):
    name = os.getenv('EMBEDDING_MODEL', 'Qwen/Qwen3-Embedding-0.6B')
    if payload.model != name:
        raise HTTPException(422, '请求的模型与本地已加载模型不一致。')
    try:
        model = local_model()
        dimensions = model.get_sentence_embedding_dimension()
        if payload.dimensions not in {None, dimensions}:
            raise HTTPException(422, '本地服务只返回模型原生维度，不截断或补齐向量。')
        with inference_lock:
            encoded = model.tokenizer(payload.input, truncation=False, return_length=True)
            if any(length > model.max_seq_length for length in encoded['length']):
                raise HTTPException(422, '输入超过本地模型的 2048 token 限制，请缩短片段或查询。')
            output = model.encode(payload.input, batch_size=4, normalize_embeddings=True,
                                  convert_to_numpy=True, show_progress_bar=False)
        return {'object': 'list', 'model': name, 'data': [
            {'object': 'embedding', 'index': i, 'embedding': vector.tolist()} for i, vector in enumerate(output)]}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, '本地 Embedding 推理失败，请检查模型、内存和运行环境。') from None
