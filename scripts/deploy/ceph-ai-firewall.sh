#!/usr/bin/env bash
set -euo pipefail

# RabbitMQ is consumed locally by ceph-ai; never expose its AMQP or
# management ports through a non-loopback host interface.
RABBIT_RULE_ARGS=(-p tcp -m multiport --dports 5672,15672)
IPTABLES=(iptables --wait 5)

# Remove every copy first. If another firewall manager preserved the rules but
# moved them below a broad ACCEPT, merely checking for existence would leave
# RabbitMQ exposed. Reinsert our exact rules at the top on every reconcile.
while "${IPTABLES[@]}" -C INPUT -i lo "${RABBIT_RULE_ARGS[@]}" -j ACCEPT 2>/dev/null; do
  "${IPTABLES[@]}" -D INPUT -i lo "${RABBIT_RULE_ARGS[@]}" -j ACCEPT
done
"${IPTABLES[@]}" -I INPUT 1 -i lo "${RABBIT_RULE_ARGS[@]}" -j ACCEPT

while "${IPTABLES[@]}" -C INPUT ! -i lo "${RABBIT_RULE_ARGS[@]}" -j REJECT --reject-with tcp-reset 2>/dev/null; do
  "${IPTABLES[@]}" -D INPUT ! -i lo "${RABBIT_RULE_ARGS[@]}" -j REJECT --reject-with tcp-reset
done
"${IPTABLES[@]}" -I INPUT 2 ! -i lo "${RABBIT_RULE_ARGS[@]}" -j REJECT --reject-with tcp-reset
