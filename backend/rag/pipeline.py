"""
RAG Pipeline
============
Handles three things:
1. Chunking — splits .md files into ~400 token segments with overlap
2. Embedding — converts text to vectors using sentence-transformers
3. Retrieval — finds the top-K most relevant chunks for a given query

Usage:
    # Seed the knowledge base (run once)
    from rag.pipeline import seed_knowledge_base
    seed_knowledge_base()

    # Retrieve context for an email
    from rag.pipeline import retrieve
    chunks = retrieve("SLA credit calculation P0 breach")
"""

import os
import glob
from typing import List
from dataclasses import dataclass
from sqlalchemy import text
from sqlalchemy.orm import Session
from sentence_transformers import SentenceTransformer

from db.models import KnowledgeChunk
from db.database import SessionLocal

# ─────────────────────────────────────────
# Config
# ─────────────────────────────────────────
KNOWLEDGE_BASE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "knowledge_base"
)
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
CHUNK_SIZE = 150       # target tokens per chunk
CHUNK_OVERLAP = 20      # overlap between chunks to avoid cutting context
TOP_K = 3               # number of chunks to retrieve per query

# Load model once at module level — expensive to reload every call
print(f"Loading embedding model: {EMBEDDING_MODEL_NAME}")
_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
print("Embedding model loaded.")


# ─────────────────────────────────────────
# Data class for retrieved results
# ─────────────────────────────────────────
@dataclass
class RetrievedChunk:
    source_doc: str
    chunk_text: str
    similarity_score: float


# ─────────────────────────────────────────
# Step 1: Chunking
# ─────────────────────────────────────────
def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """
    Splits text into overlapping chunks based on word count.
    We use words as a proxy for tokens (roughly 1 word ≈ 1.3 tokens).

    Why overlap? So that sentences spanning a chunk boundary
    aren't split in a way that loses meaning.
    """
    words = text.split()
    chunks = []
    start = 0

    while start < len(words):
        end = start + chunk_size
        chunk = " ".join(words[start:end])
        chunks.append(chunk)

        # Move forward by chunk_size minus overlap
        start += chunk_size - overlap

        # Stop if remaining words are fewer than overlap size
        # (avoids creating a tiny last chunk that's just repeated overlap)
        if start >= len(words):
            break
        if len(words) - start < overlap:
            # Grab the remaining words as a final chunk
            final_chunk = " ".join(words[start:])
            if final_chunk.strip():
                chunks.append(final_chunk)
            break

    return chunks


def load_knowledge_base_files() -> List[dict]:
    """
    Reads all .md files from the knowledge_base directory.
    Returns list of {filename, content} dicts.
    """
    kb_path = os.path.abspath(KNOWLEDGE_BASE_DIR)
    md_files = glob.glob(os.path.join(kb_path, "*.md"))

    if not md_files:
        raise FileNotFoundError(f"No .md files found in {kb_path}")

    documents = []
    for filepath in md_files:
        filename = os.path.basename(filepath)
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        documents.append({"filename": filename, "content": content})
        print(f"  Loaded: {filename} ({len(content)} chars)")

    return documents


# ─────────────────────────────────────────
# Step 2: Embedding
# ─────────────────────────────────────────
def embed(texts: List[str]) -> List[List[float]]:
    """
    Converts a list of text strings into embedding vectors.
    Returns list of float arrays, one per input text.
    """
    embeddings = _model.encode(texts, show_progress_bar=False)
    return embeddings.tolist()


# ─────────────────────────────────────────
# Step 3: Seed the database
# ─────────────────────────────────────────
def seed_knowledge_base(force: bool = False):
    """
    Reads all .md files, chunks them, embeds the chunks,
    and stores everything in the knowledge_chunks table.

    Args:
        force: If True, clears existing chunks before re-seeding.
               Use this when you update the .md files.

    Run this once during setup:
        python -c "from rag.pipeline import seed_knowledge_base; seed_knowledge_base()"
    """
    db: Session = SessionLocal()

    try:
        # Check if already seeded
        existing_count = db.query(KnowledgeChunk).count()
        if existing_count > 0 and not force:
            print(f"Knowledge base already seeded ({existing_count} chunks). "
                  f"Use force=True to re-seed.")
            return

        if force and existing_count > 0:
            print(f"Force re-seed: deleting {existing_count} existing chunks...")
            db.query(KnowledgeChunk).delete()
            db.commit()

        # Load files
        print("\nLoading knowledge base files...")
        documents = load_knowledge_base_files()

        total_chunks = 0

        for doc in documents:
            filename = doc["filename"]
            content = doc["content"]

            # Chunk the document
            chunks = chunk_text(content)
            print(f"\n  {filename}: {len(chunks)} chunks")

            # Embed all chunks for this document in one batch
            # (batching is faster than embedding one by one)
            chunk_texts = chunks
            embeddings = embed(chunk_texts)

            # Store each chunk with its embedding
            for chunk_text_content, embedding in zip(chunk_texts, embeddings):
                chunk = KnowledgeChunk(
                    source_doc=filename,
                    chunk_text=chunk_text_content,
                    embedding=embedding,
                )
                db.add(chunk)

            total_chunks += len(chunks)

        db.commit()
        print(f"\n✓ Knowledge base seeded: {total_chunks} chunks from {len(documents)} documents")

    except Exception as e:
        db.rollback()
        print(f"Error seeding knowledge base: {e}")
        raise
    finally:
        db.close()


# ─────────────────────────────────────────
# Step 4: Retrieval
# ─────────────────────────────────────────
def retrieve(query: str, top_k: int = TOP_K, db: Session = None) -> List[RetrievedChunk]:
    """
    Given a query string, finds the most semantically similar
    chunks in the knowledge base using cosine similarity.

    This is called before every LLM classification to inject
    relevant policy context into the prompt.

    Args:
        query: The search query (usually email subject + body snippet)
        top_k: Number of chunks to return (default 3)
        db: Optional database session (creates one if not provided)

    Returns:
        List of RetrievedChunk objects with text and similarity score

    Note on vector formatting:
        We format the embedding vector directly into the SQL string as a
        literal '[x, y, z]'::vector rather than using a SQLAlchemy bind
        parameter (:query_vec::vector). This avoids a known conflict where
        SQLAlchemy's parameter parser misreads the :: cast syntax as a
        malformed named parameter, causing a SyntaxError.
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        # Embed the query using the same model used for chunks
        query_embedding = embed([query])[0]

        # Format vector as a PostgreSQL literal: [0.1, 0.2, ...]
        # This bypasses the SQLAlchemy bind parameter conflict with ::vector
        vec_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

        # pgvector cosine similarity search
        # <=> operator = cosine distance (lower = more similar)
        # 1 - distance = similarity score (higher = more similar)
        results = db.execute(
            text(f"""
                SELECT
                    source_doc,
                    chunk_text,
                    1 - (embedding <=> '{vec_str}'::vector) AS similarity
                FROM knowledge_chunks
                ORDER BY embedding <=> '{vec_str}'::vector
                LIMIT :top_k
            """),
            {"top_k": top_k}
        ).fetchall()

        chunks = [
            RetrievedChunk(
                source_doc=row[0],
                chunk_text=row[1],
                similarity_score=round(float(row[2]), 4),
            )
            for row in results
        ]

        return chunks

    finally:
        if close_db:
            db.close()


def format_rag_context(chunks: List[RetrievedChunk]) -> str:
    """
    Formats retrieved chunks into a string for injection into LLM prompts.
    The LLM prompt will include this block so the AI knows which
    policy documents informed its response.

    Example output:
        [From: refund_policy.md | Relevance: 0.89]
        Refunds are not available after 14 days from purchase...

        ---

        [From: escalation_matrix.md | Relevance: 0.74]
        When a customer threatens public reviews...
    """
    if not chunks:
        return "No relevant policy context found."

    formatted = []
    for chunk in chunks:
        formatted.append(
            f"[From: {chunk.source_doc} | Relevance: {chunk.similarity_score}]\n"
            f"{chunk.chunk_text}"
        )

    return "\n\n---\n\n".join(formatted)