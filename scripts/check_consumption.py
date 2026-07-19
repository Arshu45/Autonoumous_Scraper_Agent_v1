#!/usr/bin/env python3
"""
scripts/check_consumption.py
===========================
Queries the corporate LiteLLM gateway with a lightweight completion request
to extract the cumulative spend from the response headers.
"""

import os
import sys
import requests
from dotenv import load_dotenv

# Load env variables from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

def check_litellm_consumption():
    api_base = os.getenv("LITELLM_API_BASE")
    api_key = os.getenv("LITELLM_API_KEY")
    model = os.getenv("VISION_LLM_MODEL") or "claude-haiku-4.5"

    if not api_base or not api_key:
        print("Error: LITELLM_API_BASE and LITELLM_API_KEY must be set in your .env file.")
        sys.exit(1)

    url = f"{api_base.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    # Send a tiny request to minimize token cost
    data = {
        "model": model,
        "messages": [
            {"role": "user", "content": "hi"}
        ],
        "max_tokens": 1
    }

    print(f"Connecting to LiteLLM Gateway: {api_base}")
    print(f"Using Model Group: {model}")
    print("Fetching consumption status...")

    try:
        response = requests.post(url, headers=headers, json=data, timeout=15)
        
        if response.status_code == 200:
            # Extract LiteLLM headers
            key_spend = response.headers.get("x-litellm-key-spend")
            last_call_cost = response.headers.get("x-litellm-response-cost")
            version = response.headers.get("x-litellm-version", "Unknown")
            
            print("\n" + "=" * 45)
            print("         LITELLM KEY CONSUMPTION STATUS        ")
            print("=" * 45)
            print(f"LiteLLM Gateway Version: {version}")
            if key_spend is not None:
                print(f"Cumulative Key Spend:    ${float(key_spend):.6f} USD")
            else:
                print("Cumulative Key Spend:    Not returned in response headers.")
                
            if last_call_cost is not None:
                print(f"Last Call Cost:          ${float(last_call_cost):.6f} USD")
            print("=" * 45)
            print("\nNote: Cumulative spend tracking requires an active DB behind the LiteLLM Proxy.")
        
        elif response.status_code == 429:
            print("\n[ALERT] Budget status check returned HTTP 429: Budget/Rate limit exceeded!")
            print(response.text)
        else:
            print(f"\nFailed to query completions. HTTP status: {response.status_code}")
            print(response.text)
            
    except Exception as e:
        print(f"\nAn error occurred while connecting to the proxy: {e}")

if __name__ == "__main__":
    check_litellm_consumption()
