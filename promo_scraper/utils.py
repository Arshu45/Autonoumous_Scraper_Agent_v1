import logging

logger = logging.getLogger(__name__)

def extract_and_log_metrics(response, use_litellm: bool, call_type: str, default_cost: float = 0.0) -> float:
    """
    Extracts token usage, response cost, and key spend from direct Gemini or LiteLLM responses,
    logs the metrics, and returns the actual cost of the call.
    """
    actual_cost = default_cost
    tokens_log = ""
    spend_log = ""

    if use_litellm:
        try:
            # Extract actual token usage
            p_tokens = getattr(response.usage, "prompt_tokens", 0)
            c_tokens = getattr(response.usage, "completion_tokens", 0)
            t_tokens = getattr(response.usage, "total_tokens", 0)
            tokens_log = f"prompt_tokens={p_tokens}, completion_tokens={c_tokens}, total_tokens={t_tokens}"
            
            # Retrieve cost and cumulative spend from LiteLLM headers/hidden params
            resp_headers = getattr(response, "_response_headers", {})
            raw_cost = resp_headers.get("x-litellm-response-cost") or getattr(response, "_hidden_params", {}).get("response_cost")
            if raw_cost is not None:
                actual_cost = float(raw_cost)
            
            raw_spend = resp_headers.get("x-litellm-key-spend")
            if raw_spend is not None:
                spend_log = f" | cumulative_key_spend=${float(raw_spend):.6f}"
        except Exception as e:
            logger.debug("Failed to extract LiteLLM metrics: %s", e)
    else:
        # Extract usage for direct Gemini
        try:
            usage = getattr(response, "usage_metadata", None)
            if usage:
                p_tokens = getattr(usage, "prompt_token_count", 0)
                c_tokens = getattr(usage, "candidates_token_count", 0)
                t_tokens = getattr(usage, "total_token_count", 0)
                tokens_log = f"prompt_tokens={p_tokens}, completion_tokens={c_tokens}, total_tokens={t_tokens}"
        except Exception as e:
            logger.debug("Failed to extract Gemini usage: %s", e)

    # Log real-time cost and tokens
    logger.info(
        "API Call Metrics [%s] - %s | cost=$%.6f%s",
        call_type, tokens_log or "tokens=unknown", actual_cost, spend_log
    )

    return actual_cost
