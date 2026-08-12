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
import logging
from datetime import datetime

# Ensure the root of the project is in the python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()  # Load .env before any agent imports so os.environ has PROMO_CATEGORIES, API keys, etc.

def main():
    parser = argparse.ArgumentParser(description="Run the autonomous scraper agent.")
    parser.add_argument("--url", required=True, help="Target website URL to analyse and scrape")
    parser.add_argument("--brand", required=True, help="Brand name for this competitor")
    parser.add_argument("--requirements", default="", help="Business rules — what to extract")
    parser.add_argument("--dry-run", action="store_true", help="Run pipeline but skip registration")
    parser.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    args = parser.parse_args()

    # Configure stdout & file logging
    log_dir = os.path.join(os.getcwd(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file_path = os.path.join(log_dir, f"agent_run_{timestamp}.log")

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    
    # Configure root logger with stream and file handlers
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file_path, encoding="utf-8")
    ]
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True
    )
    
    logger = logging.getLogger("run_scraper_agent")
    logger.info("Detailed execution log saving to: %s", log_file_path)

    from agent.orchestrator import build_agent_graph
    from agent.models import AgentState

    state = AgentState(url=args.url, brand=args.brand, requirements=args.requirements)
    graph = build_agent_graph()
    result = graph.invoke(state)

    # Print summary output
    print("\n" + "="*60)
    print(f"PIPELINE COMPLETE | Brand: {args.brand}")
    print(f"Status: {result.get('status')}")
    if result.get('validation_report'):
        report = result['validation_report']
        print(f"Confidence Score: {report.confidence_score}/100")
        print(f"Recommendation: {report.recommendation}")
        if report.issues:
            print(f"Issues: {report.issues}")
    elif result.get('error'):
        print(f"Error: {result['error']}")
    print(f"Full execution log: {log_file_path}")
    print("="*60 + "\n")

if __name__ == "__main__":
    main()
