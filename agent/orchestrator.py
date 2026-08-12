# agent/orchestrator.py

import logging
from langgraph.graph import StateGraph, END
from agent.models import AgentState

logger = logging.getLogger(__name__)

def run_exploration_agent(state: AgentState) -> AgentState:
    logger.info("==========================================================================")
    logger.info(">>> [AGENT PIPELINE] STEP 1: EXPLORATION AGENT | Brand: '%s' | URL: '%s'", state.brand, state.url)
    logger.info("==========================================================================")
    state.status = "exploration"
    try:
        from agent.exploration_agent import explore_site
        site_analysis = explore_site(state.url, state.brand)
        state.site_analysis = site_analysis
    except Exception as e:
        logger.exception("Exploration agent failed on url=%s", state.url)
        state.status = "failed"
        state.error = str(e)
    return state

def run_generation_agent(state: AgentState) -> AgentState:
    logger.info("==========================================================================")
    logger.info(">>> [AGENT PIPELINE] STEP 2: CONFIG GENERATION AGENT | Brand: '%s'", state.brand)
    logger.info("==========================================================================")
    try:
        from agent.generation_agent import generate_scraper_config
        state = generate_scraper_config(state)
    except Exception as e:
        logger.exception("Generation agent failed on brand=%s", state.brand)
        state.status = "failed"
        state.error = str(e)
    return state

def run_validation_agent(state: AgentState) -> AgentState:
    logger.info("==========================================================================")
    logger.info(">>> [AGENT PIPELINE] STEP 3: SANDBOX VALIDATION AGENT | Brand: '%s'", state.brand)
    logger.info("==========================================================================")
    try:
        from agent.validation_agent import run_validation_agent as _run
        return _run(state)
    except Exception as e:
        logger.exception("Validation agent failed on brand=%s", state.brand)
        state.status = "failed"
        state.error = str(e)
        return state

def run_registration(state: AgentState) -> AgentState:
    logger.info("==========================================================================")
    logger.info(">>> [AGENT PIPELINE] STEP 4: AUTO REGISTRATION AGENT | Brand: '%s'", state.brand)
    logger.info("==========================================================================")
    try:
        from agent.registration_agent import run_registration as _run
        return _run(state)
    except Exception as e:
        logger.exception("Registration agent failed on brand=%s", state.brand)
        state.status = "failed"
        state.error = str(e)
        return state

def route_after_exploration(state: AgentState) -> str:
    """Short-circuit to END if exploration failed — avoids wasting a Docker sandbox run."""
    if state.status == "failed":
        logger.warning(
            "[ROUTER] Exploration failed for brand=%s (error: %s). Short-circuiting pipeline.",
            state.brand, state.error,
        )
        return "failed"
    logger.info("[ROUTER] Exploration successful for brand=%s. Proceeding to Generation Agent.", state.brand)
    return "ok"


def route_after_generation(state: AgentState) -> str:
    """Short-circuit to END if generation failed — avoids launching the sandbox with an empty config."""
    if state.status == "failed":
        logger.warning(
            "[ROUTER] Generation failed for brand=%s (error: %s). Short-circuiting pipeline.",
            state.brand, state.error,
        )
        return "failed"
    logger.info("[ROUTER] Config Generation successful for brand=%s. Proceeding to Sandbox Validation Agent.", state.brand)
    return "ok"


def route_after_validation(state: AgentState) -> str:
    # If validation_report is None or doesn't exist, default to reject
    if not state.validation_report:
        logger.warning("[ROUTER] No validation report found. Routing to REJECT.")
        return "reject"

    if state.validation_report.sandbox_violations:
        logger.warning("[ROUTER] Sandbox violations detected (%s). Routing to REJECT.", state.validation_report.sandbox_violations)
        return "reject"

    score = state.validation_report.confidence_score
    if score >= 90:
        logger.info("[ROUTER] Validation score >= 90 (%d). Routing to AUTO_APPROVE & Registration Agent.", score)
        return "auto_approve"
    elif score >= 70:
        logger.info("[ROUTER] Validation score 70-89 (%d). Routing to PENDING human review.", score)
        return "pending"
    else:
        logger.info("[ROUTER] Validation score < 70 (%d). Routing to REJECT.", score)
        return "reject"

def build_agent_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("exploration",       run_exploration_agent)
    graph.add_node("generation",        run_generation_agent)
    graph.add_node("validation",        run_validation_agent)
    graph.add_node("registration",      run_registration)

    graph.set_entry_point("exploration")

    # Short-circuit to END immediately if exploration or generation fails.
    # This prevents wasting a Docker sandbox run on a config that will be rejected.
    graph.add_conditional_edges(
        "exploration",
        route_after_exploration,
        {
            "ok":     "generation",
            "failed": END,
        }
    )

    graph.add_conditional_edges(
        "generation",
        route_after_generation,
        {
            "ok":     "validation",
            "failed": END,
        }
    )

    graph.add_conditional_edges(
        "validation",
        route_after_validation,
        {
            "auto_approve": "registration",
            "pending":      END,   # Streamlit UI handles human review
            "reject":       END,
        }
    )

    graph.add_edge("registration", END)
    
    return graph.compile()

def run_agent_pipeline(url: str, brand: str, requirements: str = "") -> dict:
    """
    Executes the agent graph in a standalone function safely callable from
    ProcessPoolExecutor without importing Streamlit or UI modules.
    """
    state = AgentState(url=url, brand=brand, requirements=requirements)
    graph = build_agent_graph()
    res = graph.invoke(state)
    
    if hasattr(res, "model_dump"):
        return res.model_dump()
    elif isinstance(res, dict):
        out = dict(res)
        if "validation_report" in out and out["validation_report"] is not None:
            vr = out["validation_report"]
            if hasattr(vr, "model_dump"):
                out["validation_report"] = vr.model_dump()
        return out
    return res

