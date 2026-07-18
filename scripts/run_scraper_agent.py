# scripts/run_scraper_agent.py

"""
CLI entry point for the scraper agent.

Usage:
    # Run full agent pipeline for a new site
    python scripts/run_scraper_agent.py --url "https://www.example.com/sale" --brand "Example Brand"

    # With custom requirements
    python scripts/run_scraper_agent.py --url "https://www.example.com/sale" --brand "Example Brand" \
        --requirements "Focus on clothing and shoes promotions only"

    # Dry run — explore + generate + validate, but do NOT register even if auto-approved
    python scripts/run_scraper_agent.py --url "https://www.example.com/sale" --brand "Example Brand" --dry-run
"""

import argparse
import sys
import os

# Ensure the root of the project is in the python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    parser = argparse.ArgumentParser(description="Run the autonomous scraper agent.")
    parser.add_argument("--url", required=True, help="Target website URL to analyse and scrape")
    parser.add_argument("--brand", required=True, help="Brand name for this competitor")
    parser.add_argument("--requirements", default="", help="Business rules — what to extract")
    parser.add_argument("--dry-run", action="store_true", help="Run pipeline but skip registration")
    args = parser.parse_args()

    from agent.orchestrator import build_agent_graph
    from agent.models import AgentState

    state = AgentState(url=args.url, brand=args.brand, requirements=args.requirements)
    graph = build_agent_graph()
    result = graph.invoke(state)

    # Print results
    print(f"Status: {result.get('status')}")
    if result.get('validation_report'):
        report = result['validation_report']
        print(f"Confidence: {report.confidence_score}")
        print(f"Recommendation: {report.recommendation}")
    elif result.get('error'):
        print(f"Error: {result['error']}")

if __name__ == "__main__":
    main()
