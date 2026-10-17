from crypto_trader.risk.kill_switch import KillSwitch
from crypto_trader.storage.ops_db import OpsDB


def test_persists_across_restart(tmp_path):
    p = tmp_path / "ops.sqlite"
    ks = KillSwitch(OpsDB(p, "PAPER"))
    ks.trigger("MANUAL", "operator")
    ks2 = KillSwitch(OpsDB(p, "PAPER"))  # "restart"
    assert ks2.active and ks2.status()["reasons"][0]["code"] == "MANUAL"


def test_sticky_reasons_do_not_auto_clear_but_transient_do(tmp_path):
    ks = KillSwitch(OpsDB(tmp_path / "o.sqlite", "PAPER"))
    ks.trigger("STALE_MARKET_DATA", "x")
    ks.clear_transient("STALE_MARKET_DATA")
    assert not ks.active
    ks.trigger("CUM_LOSS", "y")
    ks.clear_transient("CUM_LOSS")
    assert ks.active
    ks.reset("tester")
    assert not ks.active


def test_modes_are_isolated(tmp_path):
    p = tmp_path / "o.sqlite"
    KillSwitch(OpsDB(p, "PAPER")).trigger("MANUAL", "paper only")
    assert not KillSwitch(OpsDB(p, "LIVE")).active
