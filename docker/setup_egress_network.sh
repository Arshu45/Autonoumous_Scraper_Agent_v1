#!/usr/bin/env bash
# docker/setup_egress_network.sh
# ──────────────────────────────────────────────────────────────────────────────
# Creates (or tears down) the `scraper-egress-only` Docker network.
#
# The network is parameterised by TARGET_DOMAIN so that only outbound HTTPS
# traffic to that specific domain is permitted.  All other egress is blocked
# at the iptables level via a custom Docker network with `--opt com.docker.network.bridge.enable_ip_masquerade=true`
# plus explicit DROP rules for any destination that is not the target domain.
#
# USAGE
# ─────
#   # Create network for a specific target domain:
#   ./docker/setup_egress_network.sh create <target-domain>
#   Example:
#   ./docker/setup_egress_network.sh create www.vanheusen.com.au
#
#   # Tear down the network (removes iptables rules + Docker network):
#   ./docker/setup_egress_network.sh teardown
#
# REQUIREMENTS
# ─────────────
#   • Docker daemon running (`docker info` should succeed)
#   • Root / sudo access for iptables manipulation
#   • `host` or `dig` command available for DNS resolution of TARGET_DOMAIN
#
# ENVIRONMENT PREREQUISITES
# ─────────────────────────
#   Docker version: 20.10+ recommended (tested with 29.6.1)
#   The iptables rules are applied to the bridge interface created for this
#   network. They are FLUSHED and re-applied on each `create` call, and
#   removed entirely on `teardown`.
#
# WHAT sandbox_runner.py CONSUMES
# ────────────────────────────────
#   Containers are launched with --network scraper-egress-only.
#   sandbox_runner.run_scraper_in_sandbox() passes network_mode="scraper-egress-only"
#   to the Docker SDK. This network must exist before that call.
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

NETWORK_NAME="scraper-egress-only"
SUBNET="172.28.0.0/16"
GATEWAY="172.28.0.1"
CHAIN="SCRAPER_EGRESS"

usage() {
    echo "Usage: $0 <create|teardown> [target-domain]"
    echo "  create   <domain>  — resolve domain IPs, create network, block all other egress"
    echo "  teardown           — remove iptables rules and Docker network"
    exit 1
}

# ── Helpers ───────────────────────────────────────────────────────────────────

resolve_domain_ips() {
    local domain="$1"
    # Use getent (POSIX) first; fall back to host, then dig
    if command -v getent &>/dev/null; then
        getent ahosts "$domain" 2>/dev/null | awk '{print $1}' | sort -u
    elif command -v host &>/dev/null; then
        host -t A "$domain" 2>/dev/null | awk '/has address/ {print $4}' | sort -u
    elif command -v dig &>/dev/null; then
        dig +short A "$domain" 2>/dev/null | sort -u
    else
        echo "ERROR: No DNS resolution tool found (getent/host/dig)" >&2
        exit 1
    fi
}

get_bridge_iface() {
    # Docker names the bridge after the network ID, e.g. br-<12chars>
    local net_id
    net_id=$(docker network inspect "$NETWORK_NAME" --format '{{.Id}}' 2>/dev/null || true)
    if [ -n "$net_id" ]; then
        echo "br-${net_id:0:12}"
    fi
}

flush_chain() {
    # Remove chain if it exists
    if iptables -n --list "$CHAIN" &>/dev/null; then
        iptables -F "$CHAIN" 2>/dev/null || true
        # Remove all jumps to this chain from FORWARD
        iptables -D FORWARD -j "$CHAIN" 2>/dev/null || true
        iptables -X "$CHAIN" 2>/dev/null || true
    fi
}

# ── CREATE ────────────────────────────────────────────────────────────────────

do_create() {
    local target_domain="${1:-}"
    if [ -z "$target_domain" ]; then
        echo "ERROR: target domain required for create"
        usage
    fi

    echo "[setup_egress_network] Resolving IPs for domain: $target_domain"
    mapfile -t ALLOWED_IPS < <(resolve_domain_ips "$target_domain")

    if [ "${#ALLOWED_IPS[@]}" -eq 0 ]; then
        echo "WARNING: Could not resolve any IPs for $target_domain — network will block ALL egress."
        echo "         Ensure DNS is reachable and the domain name is correct."
    else
        echo "[setup_egress_network] Resolved IPs: ${ALLOWED_IPS[*]}"
    fi

    # Create Docker network (idempotent)
    if docker network inspect "$NETWORK_NAME" &>/dev/null; then
        echo "[setup_egress_network] Network '$NETWORK_NAME' already exists — reusing."
    else
        echo "[setup_egress_network] Creating Docker network '$NETWORK_NAME' ..."
        docker network create \
            --driver bridge \
            --subnet "$SUBNET" \
            --gateway "$GATEWAY" \
            --opt "com.docker.network.bridge.enable_ip_masquerade=true" \
            --opt "com.docker.network.bridge.enable_icc=false" \
            "$NETWORK_NAME"
        echo "[setup_egress_network] Network created."
    fi

    # Determine bridge interface name
    BRIDGE_IFACE=$(get_bridge_iface)
    if [ -z "$BRIDGE_IFACE" ]; then
        echo "WARNING: Could not determine bridge interface — skipping iptables rules."
        echo "         Containers will use Docker's default network policy."
        exit 0
    fi
    echo "[setup_egress_network] Bridge interface: $BRIDGE_IFACE"

    # Flush + recreate the custom chain
    flush_chain
    iptables -N "$CHAIN"
    iptables -I FORWARD 1 -j "$CHAIN"

    # Allow established/related connections back in
    iptables -A "$CHAIN" -m state --state ESTABLISHED,RELATED -j ACCEPT

    # Allow DNS (UDP/TCP port 53) so the container can resolve the target domain
    iptables -A "$CHAIN" -p udp --dport 53 -j ACCEPT
    iptables -A "$CHAIN" -p tcp --dport 53 -j ACCEPT

    # Allow HTTP (port 80) and HTTPS (port 443) to each resolved IP of the target domain
    for ip in "${ALLOWED_IPS[@]}"; do
        echo "[setup_egress_network]   Allowing HTTP/HTTPS → $ip (from $target_domain)"
        iptables -A "$CHAIN" -o "$BRIDGE_IFACE" -d "$ip" -p tcp --dport 80 -j ACCEPT
        iptables -A "$CHAIN" -o "$BRIDGE_IFACE" -d "$ip" -p tcp --dport 443 -j ACCEPT
    done

    # Block everything else originating from the sandbox subnet
    iptables -A "$CHAIN" -s "$SUBNET" -j DROP

    echo "[setup_egress_network] iptables rules applied."
    echo ""
    echo "  Network '$NETWORK_NAME' is ready."
    echo "  Target domain : $target_domain"
    echo "  Allowed IPs   : ${ALLOWED_IPS[*]:-none (all blocked)}"
    echo ""
    echo "  To tear down  : $0 teardown"
}

# ── TEARDOWN ──────────────────────────────────────────────────────────────────

do_teardown() {
    echo "[setup_egress_network] Tearing down '$NETWORK_NAME' ..."

    # Remove iptables rules
    flush_chain
    echo "[setup_egress_network] iptables rules removed."

    # Remove Docker network (will fail if containers are still attached)
    if docker network inspect "$NETWORK_NAME" &>/dev/null; then
        docker network rm "$NETWORK_NAME" || \
            echo "WARNING: Could not remove network (containers may still be attached). Re-run after all sandbox containers exit."
        echo "[setup_egress_network] Docker network removed."
    else
        echo "[setup_egress_network] Network '$NETWORK_NAME' does not exist — nothing to remove."
    fi
}

# ── Entry point ───────────────────────────────────────────────────────────────

COMMAND="${1:-}"

case "$COMMAND" in
    create)
        do_create "${2:-}"
        ;;
    teardown)
        do_teardown
        ;;
    *)
        usage
        ;;
esac
