"""signals tests: /proc parsing, LSN differencing, live-instance observation mapping (FakeConn mock, no live instance needed)."""
import pytest

from pgtuner import signals

MEMINFO = ("MemTotal:        8132520 kB\n"
           "MemFree:          339276 kB\n"
           "MemAvailable:    1268396 kB\n"
           "Buffers:           58548 kB\n"
           "Cached:          1221440 kB\n")
LOADAVG = "0.25 0.19 0.17 1/751 1814982"


# ---------------------------- FakeConn: dispatches mock rows by SQL keyword ----------------------------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._bucket: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        for key, queue in self._conn.responses.items():
            if key in sql:
                self._bucket = queue
                return
        raise AssertionError(f"未 mock 的 SQL: {sql}")

    def fetchone(self):
        return self._bucket.pop(0)

    def fetchall(self):
        rows = list(self._bucket)
        self._bucket.clear()
        return rows


class FakeConn:
    def __init__(self, responses):
        self.responses = responses

    def cursor(self):
        return FakeCursor(self)


def fake_responses(**over):
    r = {
        "pg_settings": [
            ("server_version", "20devel", None, "internal"),
            ("server_version_num", "200000", None, "internal"),
            ("max_connections", "77", None, "user"),
            ("shared_buffers", "16384", "8kB", "postmaster"),
            ("work_mem", "4096", "kB", "user"),
            ("random_page_cost", "4", None, "user"),
            ("debug_assertions", "on", None, "internal"),
        ],
        "pg_database_size": [(171798691840,)],          # 160 GB
        "pg_stat_database": [(504426, 41134, 4, 20062208)],
        "pg_stat_user_tables": [(1020186, 1000000, 11616)],
        "pg_current_wal_lsn": [("0/100000",), ("0/200000",)],   # 1MB advanced between the two samples
    }
    r.update(over)
    return r


def collect_with(responses=None, cpu_cores=16, wal=0,
                 meminfo=MEMINFO, loadavg=LOADAVG):
    return signals.collect(FakeConn(responses or fake_responses()), cpu_cores,
                           wal_sample_seconds=wal,
                           meminfo_text=meminfo, loadavg_text=loadavg)


# ---------------------------- /proc pure functions ----------------------------

def test_parse_meminfo_and_values():
    mi = signals.parse_meminfo(MEMINFO)
    assert mi["MemAvailable"] == 1268396 and mi["Cached"] == 1221440
    gb, kb = signals.meminfo_values(mi)
    assert kb == 1268396 + 1221440                  # MemAvailable+Cached, NoCommit
    assert gb == round(kb / 1048576, 2)


def test_meminfo_missing_metric_not_injected():
    gb, kb = signals.meminfo_values({"MemTotal": 8132520})
    assert gb is None and kb is None


def test_parse_loadavg_and_cpu_saturated():
    assert signals.parse_loadavg(LOADAVG) == 0.25
    assert signals.parse_loadavg("") is None
    assert signals.cpu_is_saturated(0.25, 16) is False
    assert signals.cpu_is_saturated(13.0, 16) is True
    assert signals.cpu_is_saturated(12.8, 16) is False   # threshold cores×0.8 excludes equality
    assert signals.cpu_is_saturated(None, 16) is None


def test_lsn_to_int():
    assert signals.lsn_to_int("0/0") == 0
    assert signals.lsn_to_int("0/100000") == 1024 * 1024
    assert signals.lsn_to_int("1/0") == 1 << 32


# ---------------------------- collect mapping ----------------------------

def test_collect_maps_observations():
    sig = collect_with(cpu_cores=16, wal=0)
    obs = sig.observations
    assert obs["buffer_cache_hit_ratio"] == round(504426 / (504426 + 41134), 4)
    assert obs["n_live_tup"] == 1020186
    assert obs["biggest_table_live_tuples"] == 1000000
    assert obs["target_dead_tuples"] == 10000                     # 1% heuristic (documented)
    assert obs["wal_rate_mbps"] > 0                               # LSN advanced 1MB
    assert obs["os_free_and_cached_gb"] == round((1268396 + 1221440) / 1048576, 2)
    assert obs["freeable_mem_kb"] == 1268396 + 1221440
    assert obs["cpu_saturated"] is False
    assert sig.pg_major == 20 and sig.runtime_max_connections == 77
    assert sig.debug_build is True
    assert sig.db_size_gb == 160.0
    assert any("temp_files" in n for n in sig.notes)              # temp spill pressure note
    # current-value display: for the panel and diff
    assert sig.settings["work_mem"]["context"] == "user"


def test_collect_zero_denominator_skips_ratio():
    sig = collect_with(fake_responses(**{"pg_stat_database": [(0, 0, 0, 0)],
                                         "pg_current_wal_lsn": [("0/1",), ("0/1",)]}), wal=0)
    assert "buffer_cache_hit_ratio" not in sig.observations
    assert sig.cache_hit_ratio is None


def test_collect_wal_idle_injects_zero():
    sig = collect_with(fake_responses(**{"pg_current_wal_lsn": [("0/1",), ("0/1",)]}), wal=0)
    assert sig.observations["wal_rate_mbps"] == 0.0               # no write load, injected faithfully


def test_collect_zero_tables_no_tuple_vars():
    sig = collect_with(fake_responses(**{"pg_stat_user_tables": [(0, 0, 0)]}), wal=0)
    for k in ("n_live_tup", "biggest_table_live_tuples", "target_dead_tuples"):
        assert k not in sig.observations


def test_collect_cpu_saturated_true():
    sig = collect_with(loadavg="13.5 10.2 8.0 1/751 123")
    assert sig.observations["cpu_saturated"] is True


# ------------------- WAL sampling window + data-disk free space (heavy-load verification fix) -------------------

def test_wal_sample_default_window_is_6s():
    """Default sampling window 3s→6s: under mixed loads a short window can land exactly on an analytic blocking stretch and read 0 (improvement #2)."""
    assert signals.WAL_SAMPLE_SECONDS == 6.0
    sig = collect_with(wal=0)
    assert sig.wal_sample_seconds == 0            # the window parameter passes through as-is (no sleep)


def test_collect_data_directory_overrides_disk_free(monkeypatch):
    """A live instance's data_directory disk, measured via statvfs, takes precedence over probing the default path (overriding the ns via observations)."""
    monkeypatch.setattr(signals, "_disk_free_gb", lambda p: 42.5)
    responses = fake_responses()
    responses["pg_settings"] = responses["pg_settings"] + [
        ("data_directory", "/var/lib/postgresql/18/main", None, "internal")]
    sig = collect_with(responses, wal=0)
    assert sig.observations["disk_free_gb"] == 42.5


def test_collect_without_data_directory_no_disk_free(monkeypatch):
    """No data_directory row in pg_settings (or statvfs fails) → not injected; clamping uses the probed value or is skipped."""
    monkeypatch.setattr(signals, "_disk_free_gb", lambda p: 42.5)
    sig = collect_with(wal=0)                     # fake pg_settings contains no data_directory
    assert "disk_free_gb" not in sig.observations


# ---------------------------- display / conversion helpers ----------------------------

def test_display_value():
    assert signals.display_value("16384", "8kB") == "128MB"
    assert signals.display_value("4096", "kB") == "4MB"
    assert signals.display_value("512", "kB") == "512kB"
    assert signals.display_value("2048", "MB") == "2GB"
    assert signals.display_value("3000", "ms") == "3000ms"
    assert signals.display_value("on", None) == "on"
    assert signals.display_value("4", None) == "4"


def test_setting_kb():
    settings = {
        "work_mem": {"setting": "4096", "unit": "kB", "context": "user"},
        "shared_buffers": {"setting": "16384", "unit": "8kB", "context": "postmaster"},
        "random_page_cost": {"setting": "4", "unit": None, "context": "user"},
    }
    assert signals.setting_kb(settings, "work_mem") == 4096
    assert signals.setting_kb(settings, "shared_buffers") == 131072
    assert signals.setting_kb(settings, "random_page_cost") is None
    assert signals.setting_kb(settings, "nope") is None
