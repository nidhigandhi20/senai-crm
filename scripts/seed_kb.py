"""
Seed Knowledge Base Script
==========================
Run this once after setting up the database to embed
all policy documents and store them in PostgreSQL.

Usage (from project root):
    cd senai-crm
    python scripts/seed_kb.py

Re-seed after updating .md files:
    python scripts/seed_kb.py --force
"""

import sys
import os
import argparse

# Add backend to path so we can import from it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from rag.pipeline import seed_knowledge_base, retrieve, format_rag_context


def main():
    parser = argparse.ArgumentParser(description="Seed the RAG knowledge base")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-seed even if chunks already exist"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="After seeding, run test queries to verify retrieval works"
    )
    args = parser.parse_args()

    print("=" * 50)
    print("SenAI CRM — Knowledge Base Seeder")
    print("=" * 50)

    # Seed the knowledge base
    seed_knowledge_base(force=args.force)

    # Optionally test retrieval
    if args.test:
        print("\n" + "=" * 50)
        print("Testing retrieval...")
        print("=" * 50)

        test_queries = [
            # Should retrieve refund_policy.md + escalation_matrix.md
            "I want a refund and I will post a review on Trustpilot",

            # Should retrieve sla_policy.md
            "SLA breach 47 minutes downtime credit calculation P0",

            # Should retrieve pricing_policy.md
            "non-profit discount 501c3 standard plan seats",

            # Should retrieve compliance_faq.md
            "GDPR Article 20 data portability request personal data export",

            # Should retrieve api_docs.md
            "403 error v2 endpoint missing header integration",
        ]

        for query in test_queries:
            print(f"\nQuery: '{query[:60]}...'")
            chunks = retrieve(query)
            for chunk in chunks:
                print(f"  → {chunk.source_doc} (score: {chunk.similarity_score})")


if __name__ == "__main__":
    main()