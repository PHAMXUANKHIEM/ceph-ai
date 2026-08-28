from watcher.node_metrics import parse_node_metrics


def test_parse_node_metrics_includes_network_rates_without_counting_loopback():
    raw = """===CPU1===
cpu  100 0 100 800 0 0 0 0
===DISK1===
  8 0 sda 1 0 1 1 1 0 1 1 0 0 0
===NET1===
Inter-| Receive | Transmit
 lo: 100 0 0 0 0 0 0 0 100 0 0 0 0 0 0 0
 eth0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0
===CPU2===
cpu  100 0 100 900 0 0 0 0
===DISK2===
  8 0 sda 1 0 1 1 1 0 1 1 0 0 0
===NET2===
Inter-| Receive | Transmit
 lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0
 eth0: 1600 0 0 0 0 0 0 0 2800 0 0 0 0 0 0 0
===MEM===
MemTotal: 1000 kB
MemAvailable: 500 kB
"""
    result = parse_node_metrics(raw)
    assert result["network_rx_bytes_per_sec"] == 600.0
    assert result["network_tx_bytes_per_sec"] == 800.0
