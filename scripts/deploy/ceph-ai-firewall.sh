#!/usr/bin/env bash
set -euo pipefail

# RabbitMQ is consumed locally by ceph-ai; never expose its AMQP or
# management ports through a non-loopback host interface.
RABBIT_RULE_ARGS=(-p tcp -m multiport --dports 5672,15672)

if ! iptables -C INPUT -i lo "${RABBIT_RULE_ARGS[@]}" -j ACCEPT 2>/dev/null; then
  iptables -I INPUT 1 -i lo "${RABBIT_RULE_ARGS[@]}" -j ACCEPT
fi

if ! iptables -C INPUT ! -i lo "${RABBIT_RULE_ARGS[@]}" -j REJECT --reject-with tcp-reset 2>/dev/null; then
  iptables -I INPUT 2 ! -i lo "${RABBIT_RULE_ARGS[@]}" -j REJECT --reject-with tcp-reset
fi
