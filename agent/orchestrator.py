# agent/orchestrator.py

import logging
from langgraph.graph import StateGraph, END
from agent.models import AgentState

logger = logging.getLogger(__name__)

def run_exploration_agent(state: AgentState) -> AgentState:
    logger.info("Running exploration agent on url=%s", state.url)
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
    logger.info("Running generation agent on brand=%s", state.brand)
    try:
        from agent.generation_agent import generate_scraper_config
        state = generate_scraper_config(state)
    except Exception as e:
        logger.exception("Generation agent failed on brand=%s", state.brand)
        state.status = "failed"
        state.error = str(e)
    return state

def run_validation_agent(state: AgentState) -> AgentState:
    from agent.validation_agent import run_validation_agent as _run
    return _run(state)

def run_registration(state: AgentState) -> AgentState:
    from agent.registration_agent import run_registration as _run
    return _run(state)

def route_after_validation(state: AgentState) -> str:
    # If validation_report is None or doesn't exist, default to reject
    if not state.validation_report:
        logger.warning("No validation report found. Routing to reject.")
        return "reject"
        
    if state.validation_report.sandbox_violations:
        logger.warning("Sandbox violations detected. Routing to reject.")
        return "reject"
        
    score = state.validation_report.confidence_score
    if score >= 90:
        logger.info("Validation score >= 90 (%d). Routing to auto_approve.", score)
        return "auto_approve"
    elif score >= 70:
        logger.info("Validation score 70-89 (%d). Routing to pending.", score)
        return "pending"
    else:
        logger.info("Validation score < 70 (%d). Routing to reject.", score)
        return "reject"

def build_agent_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("exploration",       run_exploration_agent)
    graph.add_node("generation",        run_generation_agent)
    graph.add_node("validation",        run_validation_agent)
    graph.add_node("registration",      run_registration)

    graph.set_entry_point("exploration")

    graph.add_edge("exploration", "generation")
    graph.add_edge("generation",  "validation")

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
