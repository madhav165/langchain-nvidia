"""Test NVIDIA AI Foundation Model Embeddings.

Note: These tests are designed to validate the functionality of NVIDIAEmbeddings.
"""

import requests_mock

from langchain_nvidia_ai_endpoints import NVIDIAEmbeddings


def test_nvai_play_embedding_documents(embedding_model: str) -> None:
    """Test NVIDIA embeddings for documents."""
    documents = ["foo bar"]
    embedding = NVIDIAEmbeddings(model=embedding_model)
    output = embedding.embed_documents(documents)
    assert len(output) == 1
    assert len(output[0]) == 1024  # Assuming embedding size is 2048


def test_nvai_play_embedding_documents_multiple(embedding_model: str) -> None:
    """Test NVIDIA embeddings for multiple documents."""
    documents = ["foo bar", "bar foo", "foo"]
    embedding = NVIDIAEmbeddings(model=embedding_model)
    output = embedding.embed_documents(documents)
    assert len(output) == 3
    assert all(len(doc) == 1024 for doc in output)


def test_nvai_play_embedding_query(embedding_model: str) -> None:
    """Test NVIDIA embeddings for a single query."""
    query = "foo bar"
    embedding = NVIDIAEmbeddings(model=embedding_model)
    output = embedding.embed_query(query)
    assert len(output) == 1024


async def test_nvai_play_embedding_async_query(embedding_model: str) -> None:
    """Test NVIDIA async embeddings for a single query."""
    query = "foo bar"
    embedding = NVIDIAEmbeddings(model=embedding_model)
    output = await embedding.aembed_query(query)
    assert len(output) == 1024


async def test_nvai_play_embedding_async_documents(embedding_model: str) -> None:
    """Test NVIDIA async embeddings for multiple documents."""
    documents = ["foo bar", "bar foo", "foo"]
    embedding = NVIDIAEmbeddings(model=embedding_model)
    output = await embedding.aembed_documents(documents)
    assert len(output) == 3
    assert all(len(doc) == 1024 for doc in output)


def test_embed_available_models() -> None:
    embedding = NVIDIAEmbeddings()
    models = embedding.available_models
    assert len(models) == 2  # nvolveqa_40k and ai-embed-qa-4
    assert all(model.id in ["nvolveqa_40k", "ai-embed-qa-4"] for model in models)


def test_embed_available_models_cached() -> None:
    """Test NVIDIA embeddings for available models."""
    with requests_mock.Mocker(real_http=True) as mock:
        embedding = NVIDIAEmbeddings()
        assert not mock.called
        embedding.available_models
        assert mock.called
        embedding.available_models
        embedding.available_models
        assert mock.call_count == 1
