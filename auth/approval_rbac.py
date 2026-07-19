# auth/approval_rbac.py
"""
Role-Based Access Control for the agent pipeline.

Currently a placeholder that grants AGENT_USER to all authenticated users.
Replace require_role() with a real role lookup (e.g. database, LDAP, SSO)
when team grows beyond a single operator.
"""

from enum import Enum
import os


class AgentRole(str, Enum):
    AGENT_USER = "agent_user"
    # Future: OPERATOR = "operator"  (trigger only)
    # Future: APPROVER = "approver"  (approve only)


def require_role(user_id: str, role: AgentRole) -> bool:
    """
    Placeholder — currently all identified users have AGENT_USER.
    Replace with real role lookup when team grows.
    """
    return True  # all authenticated users pass for now


def get_current_user() -> str:
    """
    Returns current user identity. Reads AGENT_USER_ID env var.
    Placeholder for future SSO integration.
    """
    return os.getenv("AGENT_USER_ID", "default_operator")
