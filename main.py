
"""RAG pipeline for PDF search with ChromaDB and Ollama LLM."""

import argparse
import os
import re
import sys

import chromadb
import requests
from chromadb.errors import NotFoundError
from pypdf import PdfReader
from sentence_transformers import SentenceTransformer

DEFAULT_PDF_PATH = os.getenv("PDF_PATH", r"C:\Users\KIIT\Downloads\preparation.pdf")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
CHROMA_PATH = "./chroma_db"
COLLECTION_NAME = "pdf_documents"
TOP_K = 3


def clean_text(text):
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def find_pdf_file(custom_path=None):
    if custom_path:
        if os.path.exists(custom_path):
            return custom_path
        raise FileNotFoundError(f"PDF not found: {custom_path}")

    if os.path.exists(DEFAULT_PDF_PATH):
        return DEFAULT_PDF_PATH

    current_dir = os.getcwd()
    for root, _, files in os.walk(current_dir):
        for filename in files:
            if filename.lower().endswith(".pdf"):
                return os.path.join(root, filename)

    raise FileNotFoundError(
        "No PDF file was found. Set PDF_PATH to a valid PDF, or pass --pdf <path>."
    )


def read_pdf(pdf_path):
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    reader = PdfReader(pdf_path)
    text = ""
    for page in reader.pages:
        page_text = page.extract_text() or ""
        text += page_text + "\n"
    return clean_text(text)


def create_chunks(text, chunk_size=600, overlap=100):
    if chunk_size <= overlap:
        raise ValueError("Chunk size must be greater than overlap")

    if not text.strip():
        return []

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


def get_collection():
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    try:
        client.delete_collection(name=COLLECTION_NAME)
    except (ValueError, NotFoundError):
        pass

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    return client, collection


def index_pdf(chunks):
    if not chunks:
        raise ValueError("No text was extracted from the PDF. Please check the file content.")

    model = SentenceTransformer(EMBEDDING_MODEL)
    embeddings = model.encode(chunks, convert_to_numpy=True, show_progress_bar=False)
    _, collection = get_collection()

    collection.add(
        documents=chunks,
        embeddings=embeddings.tolist(),
        ids=[f"chunk-{i}" for i in range(len(chunks))],
    )
    print(f"Indexed {len(chunks)} chunks.")
    return model, collection


def retrieve_context(query, model, collection, top_k=None):
    if top_k is None:
        top_k = TOP_K

    query_embedding = model.encode(query, convert_to_numpy=True)
    results = collection.query(query_embeddings=[query_embedding.tolist()], n_results=top_k)
    documents = results.get("documents", [[]])[0]
    distances = results.get("distances", [[]])[0]
    return documents, distances


def generate_with_ollama(question, context, model_name=None):
    model_name = model_name or OLLAMA_MODEL
    prompt = (
        "Use only the following context to answer the question. "
        "If the answer is not in the context, say you do not have enough information.\n\n"
        f"Context:\n{context}\n\nQuestion:\n{question}\n\nAnswer:"
    )

    try:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={"model": model_name, "prompt": prompt, "stream": False},
            timeout=120,
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            f"Cannot reach Ollama at {OLLAMA_BASE_URL}. Install Ollama if needed, start the "
            "Ollama app (or run 'ollama serve'), then retry. "
            f"Original error: {exc}"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise RuntimeError(
            f"Ollama at {OLLAMA_BASE_URL} did not respond within 120 seconds. "
            "Check that the server is running and retry."
        ) from exc
    except requests.exceptions.HTTPError as exc:
        details = exc.response.text.strip() if exc.response is not None else str(exc)
        if exc.response is not None and exc.response.status_code == 404:
            details = f"Model '{model_name}' may not be installed. Run 'ollama pull {model_name}'."
        raise RuntimeError(f"Ollama returned an HTTP error: {details}") from exc

    payload = response.json()
    return payload.get("response", "").strip()


def run_rag(query, pdf_path=None, model_name=None, top_k=None):
    actual_pdf_path = find_pdf_file(pdf_path)
    pdf_text = read_pdf(actual_pdf_path)
    chunks = create_chunks(pdf_text)

    if not chunks:
        raise ValueError(f"No usable text was found in: {actual_pdf_path}")

    print(f"PDF found: {actual_pdf_path}")
    print(f"Total chunks: {len(chunks)}")

    embedding_model, collection = index_pdf(chunks)
    documents, distances = retrieve_context(query, embedding_model, collection, top_k=top_k)

    if not documents:
        raise RuntimeError("The document index is empty or the query returned no results.")

    print("\nTop matching chunks:")
    for i, doc in enumerate(documents, start=1):
        print(f"\nResult {i} | distance: {distances[i - 1]}")
        print(doc[:800])

    context = "\n\n---\n\n".join(documents)
    answer = generate_with_ollama(query, context, model_name=model_name)

    print("\nOllama answer:")
    print(answer)


def parse_args():
    parser = argparse.ArgumentParser(description="RAG pipeline using ChromaDB and Ollama")
    parser.add_argument("query", nargs="?", help="Question to ask the PDF")
    parser.add_argument("--pdf", dest="pdf_path", help="Path to the PDF file")
    parser.add_argument("--model", default=OLLAMA_MODEL, help="Ollama model name")
    parser.add_argument("--top-k", type=int, default=TOP_K, help="Number of relevant chunks to retrieve")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    query = args.query or input("\nEnter your question: ").strip()

    try:
        run_rag(query, pdf_path=args.pdf_path, model_name=args.model, top_k=args.top_k)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
