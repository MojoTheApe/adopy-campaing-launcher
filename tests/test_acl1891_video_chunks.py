"""ACL-1891: видео больше 200 МБ уходит в Meta кусками (resumable upload `advideos`).

Проверяется: закрытая политика загрузки в обеих линиях (Python и помощник страницы в Node),
цикл start -> transfer -> finish на поддельной Meta, докачка с названного Meta смещения,
неизвестный результат без второй загрузки, память (читаются только куски), бюджет вызовов,
потолок размера и три мутации (чужая фаза, лишний кусок, пропуск проверки смещения).

Размеры в тестах уменьшены подменой констант: порог «большого» файла - 1 МБ, кусок - 1 МБ.
"""
from __future__ import annotations

import hashlib
import re
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft  # noqa: E402

MB = 1024 * 1024
#: Настоящий потолок куска, снятый до мутаций.
REAL_CHUNK_MAX = wp.UPLOAD_CHUNK_MAX
PATH = f"/{ACT}/advideos"


# ───────────────────────── политика, Python ─────────────────────────

def _ok(params, files=None):
    return wp.check(ACT, "POST", PATH, params, files)


def _refused(params, files=None):
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", PATH, params, files)


START = {"upload_phase": "start", "file_size": "300000000"}
TRANSFER = {"upload_phase": "transfer", "upload_session_id": "12345678", "start_offset": "0"}
FINISH = {"upload_phase": "finish", "upload_session_id": "12345678", "title": "acl-x-1"}


def test_policy_allows_exactly_the_three_phases():
    assert _ok(START) == "advideos"
    assert _ok(TRANSFER, {"video_file_chunk": b"abc"}) == "advideos"
    assert _ok(FINISH) == "advideos"
    assert _ok({**FINISH, "name": "acl-x-1"}) == "advideos"
    assert _ok({**START, "file_size": 300000000}) == "advideos"


@pytest.mark.parametrize("params,files", [
    ({"upload_phase": "cancel", "upload_session_id": "12345678"}, None),
    ({"upload_phase": "bogus"}, None),
    ({"upload_phase": ["start"]}, None),
    ({**START, "upload_session_id": "12345678"}, None),                       # чужой ключ фазы
    ({**START, "file_size": "-5"}, None),
    ({**START, "file_size": "0"}, None),
    ({**START, "file_size": str(wp.UPLOAD_FILE_MAX + 1)}, None),
    ({**START, "file_size": 1.5}, None),
    ({**START, "file_size": True}, None),
    ({**START}, {"video_file_chunk": b"a"}),                                  # у start файла нет
    ({**TRANSFER, "upload_session_id": "abc"}, {"video_file_chunk": b"a"}),
    ({**TRANSFER, "upload_session_id": "12"}, {"video_file_chunk": b"a"}),
    ({**TRANSFER, "start_offset": "x1"}, {"video_file_chunk": b"a"}),
    ({**TRANSFER, "start_offset": "-1"}, {"video_file_chunk": b"a"}),
    ({**TRANSFER, "start_offset": str(wp.UPLOAD_FILE_MAX)}, {"video_file_chunk": b"a"}),
    ({**TRANSFER, "end_offset": "9"}, {"video_file_chunk": b"a"}),
    ({**TRANSFER, "status": "ACTIVE"}, {"video_file_chunk": b"a"}),
    ({**TRANSFER}, None),                                                     # без куска
    ({**TRANSFER}, {"source": b"a"}),                                         # не то поле
    ({**TRANSFER}, {"video_file_chunk": b""}),                                # пустой кусок
    ({**FINISH, "title": ""}, None),
    ({**FINISH, "source": "x"}, None),
    ({**FINISH}, {"video_file_chunk": b"a"}),                                 # у finish файла нет
    ({**FINISH, "upload_session_id": "../1"}, None),
])
def test_policy_refuses_everything_else(params, files):
    _refused(params, files)


def test_a_plain_video_post_is_unchanged():
    assert _ok({"name": "t", "title": "t"}, {"source": b"abc"}) == "advideos"
    _refused({"name": "t", "title": "t"}, {"video_file_chunk": b"abc"})
    _refused({"name": "t", "title": "t"})


def test_upload_phases_never_reach_other_edges():
    for edge in ("adimages", "campaigns", "ads"):
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", f"/{ACT}/{edge}", dict(START))


def _guard_phase():
    """Чужая фаза `cancel` обязана быть отказана."""
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", PATH, {"upload_phase": "cancel", "upload_session_id": "12345678"})
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", PATH, {"upload_phase": "cancel", "file_size": "5"})


def _guard_chunk():
    """Кусок больше потолка обязан быть отказан."""
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", PATH, TRANSFER, {"video_file_chunk": b"x" * (REAL_CHUNK_MAX + 1)})


def test_guards_hold_on_the_real_policy():
    _guard_phase()
    _guard_chunk()


# ───────────────────────── политика, помощник страницы (Node) ─────────────────────────

_NODE = r"""
const crypto = require('crypto');
const calls = [];
const fakeFetch = async (url, init) => { calls.push(String(url)); return { status: 200,
  text: async () => JSON.stringify({ id: '5' }), headers: { get: () => null } }; };
const loc = { href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns',
              hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: [],
              documentElement: { innerHTML: '"accessToken":"' + TOKENV + '"' } };
const G = {};
new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams','crypto', HELPER)(
  G, loc, doc, { getEntriesByType: () => [] }, fakeFetch, URL, URLSearchParams, crypto.webcrypto);
const h = G[NAME];
(async () => {
  const out = {};
  for (const [k, c] of Object.entries(CASES)) {
    const spec = { method: 'POST', path: PATH, params: c.params, filename: 'chunk', files: {} };
    if (c.chunk !== null && c.chunk !== undefined) {
      const bytes = Buffer.alloc(c.chunk, 7);
      const sid = 'f_' + k;
      h.stage(sid, bytes.toString('base64'));
      spec.files = { [c.field || 'video_file_chunk']: sid };
      spec.expect = { bytes: bytes.length, sha256: crypto.createHash('sha256').update(bytes).digest('hex') };
    }
    const r = await h.call(spec);
    out[k] = r.error ? { error: r.error, why: r.why } : { ok: true };
  }
  process.stdout.write(JSON.stringify(out));
})();
"""

_CASES = {
    "start": {"params": START, "chunk": None},
    "transfer": {"params": TRANSFER, "chunk": 10},
    "finish": {"params": FINISH, "chunk": None},
    "cancel": {"params": {"upload_phase": "cancel", "upload_session_id": "12345678"}, "chunk": None},
    "bogus": {"params": {"upload_phase": "bogus"}, "chunk": None},
    "start_extra": {"params": {**START, "upload_session_id": "12345678"}, "chunk": None},
    "start_big": {"params": {**START, "file_size": str(wp.UPLOAD_FILE_MAX + 1)}, "chunk": None},
    "start_zero": {"params": {**START, "file_size": "0"}, "chunk": None},
    "start_file": {"params": START, "chunk": 4},
    "bad_session": {"params": {**TRANSFER, "upload_session_id": "ab"}, "chunk": 4},
    "bad_offset": {"params": {**TRANSFER, "start_offset": "x"}, "chunk": 4},
    "neg_offset": {"params": {**TRANSFER, "start_offset": "-3"}, "chunk": 4},
    "no_chunk": {"params": TRANSFER, "chunk": None},
    "wrong_field": {"params": TRANSFER, "chunk": 4, "field": "source"},
    "big_chunk": {"params": TRANSFER, "chunk": wp.UPLOAD_CHUNK_MAX + 1},
    "empty_chunk": {"params": TRANSFER, "chunk": 0},
    "finish_file": {"params": FINISH, "chunk": 4},
    "finish_extra": {"params": {**FINISH, "source": "x"}, "chunk": None},
    "max_chunk": {"params": TRANSFER, "chunk": wp.UPLOAD_CHUNK_MAX},
    # 17.10: Meta 1363049 «A waterfall ID is required» — id загрузки во всех фазах.
    "start_waterfall": {"params": {**START, "waterfall_id": "0" * 32}, "chunk": None},
    "transfer_waterfall": {"params": {**TRANSFER, "waterfall_id": "ab" * 16}, "chunk": 4},
    "bad_waterfall": {"params": {**START, "waterfall_id": "not-hex"}, "chunk": None},
}


def _run_node(policy):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_upload_parity"
    script = (_NODE.replace("TOKENV", json.dumps("EAAB" + "Zx9" * 20)).replace("NAME", json.dumps(name))
              .replace("PATH", json.dumps(PATH)).replace("CASES", "(" + json.dumps(_CASES) + ")")
              .replace("HELPER", json.dumps(tc.helper_source(name, policy))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=120)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _python_verdict(case):
    chunk = case.get("chunk")
    files = None
    if chunk is not None:
        files = {case.get("field", "video_file_chunk"): b"\x07" * chunk}
    try:
        wp.check(ACT, "POST", PATH, case["params"], files)
        return True
    except wp.WriteRefused:
        return False


def test_the_page_helper_agrees_with_python_on_every_case():
    out = _run_node(wp.policy_for_page(ACT))
    for name, case in _CASES.items():
        allowed = _python_verdict(case)
        assert ("ok" in out[name]) == allowed, (name, out[name], allowed)
    assert all("ok" in out[k] for k in ("start", "transfer", "finish", "max_chunk"))
    assert out["big_chunk"] == {"error": "write_refused", "why": "chunk_size"}
    assert out["cancel"]["why"] == "upload_phase"


def test_the_helper_refuses_a_foreign_phase_when_the_policy_is_mutated():
    # Мутация страницы: политика с фазой cancel - тот же охранник в слое страницы поймает её.
    policy = wp.policy_for_page(ACT)
    policy["upload"]["phases"] = list(policy["upload"]["phases"]) + ["cancel"]
    policy["upload"]["keys"]["cancel"] = ["upload_phase", "upload_session_id"]
    out = _run_node(policy)
    with pytest.raises(AssertionError):
        assert "error" in out["cancel"]


# ───────────────────────── поддельная Meta с загрузкой кусками ─────────────────────────

class ChunkMeta(FakeMeta):
    """FakeMeta + resumable upload: сессии, смещения Meta, сбои по сценарию."""

    def __init__(self, *, meta_chunk=MB, **kw):
        super().__init__(**kw)
        self.meta_chunk = meta_chunk
        self.sessions: dict[str, dict] = {}
        #: очередной сбой кусков: "lost" (принят, ответа нет), "unsent" (не принят, ответа нет),
        #: "refused_stored" (принят, Meta отвечает ошибкой с нужным смещением), "bogus_ahead", "bogus_back"
        self.chunk_faults: list[str] = []
        #: сбой finish: "lost" - завершено, ответа нет
        self.finish_fault = ""
        self.phases: list[tuple[str, int]] = []

    def _range(self, sess):
        got = sess["received"]
        return {"start_offset": str(got), "end_offset": str(min(got + self.meta_chunk, sess["size"]))}

    def write(self, path, params, files=None, filename="", on_ad=False, children_active=False, **_k):
        if not (path.endswith("/advideos") and "upload_phase" in params):
            return super().write(path, params, files, filename, on_ad=on_ad, children_active=children_active, **_k)
        wp.check(ACT, "POST", path, params, files, on_ad=on_ad, children_active=children_active)
        self.posts.append((path, dict(params)))
        phase = params["upload_phase"]
        if phase == "start":
            vid = self._new("video", title=None, _polls=0)
            sid = str(900000 + len(self.sessions))
            self.sessions[sid] = {"size": int(params["file_size"]), "received": 0, "sha": hashlib.sha256(),
                                  "video": vid, "done": False}
            self.phases.append(("start", 0))
            return {"status": 200, "body": {"upload_session_id": sid, "video_id": vid,
                                            **self._range(self.sessions[sid])}}
        sess = self.sessions[params["upload_session_id"]]
        if phase == "transfer":
            offset = int(params["start_offset"])
            chunk = files["video_file_chunk"]
            self.phases.append(("transfer", offset))
            fault = self.chunk_faults.pop(0) if self.chunk_faults else ""
            if fault == "unsent":
                return {"error": "call_unknown"}
            if offset != sess["received"]:
                # Как Meta: кусок не с того места - отказ и нужное смещение.
                return {"status": 400, "body": {"error": {"code": 100, "type": "OAuthException",
                        "message": "(#100) wrong start offset", "error_data": self._range(sess)}}}
            sess["received"] += len(chunk)
            sess["sha"].update(chunk)
            if fault == "lost":
                return {"error": "call_unknown"}
            if fault == "refused_stored":
                return {"status": 400, "body": {"error": {"code": 100, "type": "OAuthException",
                        "message": "(#100) chunk already stored", "error_data": self._range(sess)}}}
            if fault == "bogus_ahead":
                return {"status": 200, "body": {"start_offset": str(sess["size"] + 10), "end_offset": str(sess["size"] + 20)}}
            if fault == "bogus_back":
                return {"status": 200, "body": {"start_offset": "0", "end_offset": str(self.meta_chunk)}}
            return {"status": 200, "body": self._range(sess)}
        assert phase == "finish"
        self.phases.append(("finish", 0))
        assert sess["received"] == sess["size"], "finish before all bytes arrived"
        sess["done"] = True
        self.objects[sess["video"]]["title"] = params.get("title")
        if self.finish_fault == "lost":
            return {"error": "call_unknown"}
        return {"status": 200, "body": {"success": True}}


@pytest.fixture
def small(monkeypatch):
    """Порог «большого» файла и кусок - 1 МБ (в проде 200 МБ и 8 МБ)."""
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES", MB)
    monkeypatch.setattr(cr, "VIDEO_CHUNK_MAX", MB)
    monkeypatch.setattr(wp, "UPLOAD_CHUNK_MAX", MB)


def _big_draft(tmp_path, size=3 * MB + 500_000):
    d = _draft(tmp_path, video=True, ads=1)
    ad = d.ad_sets[0].ads[0]
    ad.creative_type = "Video"
    f = Path(ad.creative_path)
    f.write_bytes(b"")
    # Псевдослучайное содержимое без повторов кусков.
    with open(f, "wb") as fh:
        seed = hashlib.sha256(b"acl-1891").digest()
        written = 0
        while written < size:
            seed = hashlib.sha256(seed).digest()
            block = (seed * 4096)[: min(131072, size - written)]
            fh.write(block)
            written += len(block)
    return d, f


def _run(meta, draft, *, max_calls=200, **kw):
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=max_calls)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None, **kw)
    return creator.run(cr.plan_from_draft(draft)), client


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _video_objects(meta):
    return [o for o in meta.objects.values() if o["_kind"] == "video"]


def test_a_big_video_goes_up_in_chunks_and_everything_stays_paused(small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ChunkMeta()
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert out.readback["ok"] is True
    names = [p for p, _ in meta.phases]
    assert names[0] == "start" and names[-1] == "finish" and names.count("transfer") == 4
    offsets = [o for p, o in meta.phases if p == "transfer"]
    assert offsets == [0, MB, 2 * MB, 3 * MB]
    sess = next(iter(meta.sessions.values()))
    assert sess["done"] and sess["sha"].hexdigest() == _sha(f)
    assert len(_video_objects(meta)) == 1 and _video_objects(meta)[0]["title"].startswith("acl-")
    # ACL-2114: the campaign stays PAUSED; ad sets and ads are ACTIVE under it. Uploads carry no status.
    for path, params in meta.posts:
        edge = path.rsplit("/", 1)[1]
        if edge == "campaigns":
            assert params["status"] == "PAUSED"
        elif edge in ("adsets", "ads"):
            assert params["status"] == "ACTIVE"
        else:
            assert "ACTIVE" not in json.dumps(params)
    # Целым файлом (поле source) в Meta ничего не ушло: только фазы загрузки.
    assert all("upload_phase" in p for path, p in meta.posts if path.endswith("/advideos"))


def test_only_chunk_sized_slices_are_ever_read(small, tmp_path):
    d, f = _big_draft(tmp_path)
    reads: list[int] = []

    def reader(path, offset, n):
        reads.append(n)
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read(n)

    meta = ChunkMeta()
    out, _ = _run(meta, d, read_range=reader)
    assert out.error == "", out.error
    assert reads and max(reads) <= MB < f.stat().st_size
    assert max(reads) <= max(cr.VIDEO_CHUNK_MAX, cr.LargeFile.HASH_BLOCK)
    # Целиком файл не читался ни разу: ни одного чтения размером с файл.
    assert all(n < f.stat().st_size for n in reads)


def test_large_file_refuses_a_whole_file_slice(small, tmp_path):
    d, f = _big_draft(tmp_path)
    big = cr.LargeFile(str(f), f.stat().st_size)
    with pytest.raises(ValueError):
        big[:]
    assert big[0:8] == f.read_bytes()[:8] and len(big) == f.stat().st_size
    assert big.md5() == hashlib.md5(f.read_bytes()).hexdigest()


def test_a_refusal_with_meta_offset_resumes_there_without_resending(small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ChunkMeta()
    meta.chunk_faults = ["", "refused_stored"]            # второй кусок принят, ответ - ошибка со смещением
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    offsets = [o for p, o in meta.phases if p == "transfer"]
    assert offsets == [0, MB, 2 * MB, 3 * MB] and len(_video_objects(meta)) == 1
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == _sha(f)
    assert any("resuming" in s for s in out.steps)


def test_an_unknown_chunk_is_resent_at_the_same_offset_and_never_duplicates(small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ChunkMeta()
    meta.chunk_faults = ["", "lost"]                      # второй кусок дошёл, ответ потерян
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    offsets = [o for p, o in meta.phases if p == "transfer"]
    assert offsets == [0, MB, MB, 2 * MB, 3 * MB]         # повтор ровно того же смещения
    assert len(_video_objects(meta)) == 1 and len(meta.sessions) == 1
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == _sha(f)


def test_an_unsent_chunk_is_resent_once_and_the_video_is_whole(small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ChunkMeta()
    meta.chunk_faults = ["unsent"]
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == _sha(f)


def test_a_lost_finish_is_reconciled_by_the_title_not_uploaded_again(small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ChunkMeta()
    meta.finish_fault = "lost"
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert [p for p, _ in meta.phases].count("start") == 1 and len(_video_objects(meta)) == 1
    assert any("reconciled" in s for s in out.steps)


def test_a_rerun_reuses_the_uploaded_video(small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ChunkMeta()
    first, _ = _run(meta, d)
    assert first.error == ""
    before = len(meta.phases)
    second, client = _run(meta, d)
    assert second.error == "" and second.created == {}
    assert len(meta.phases) == before and len(_video_objects(meta)) == 1 and client.writes == 0


def test_a_stuck_chunk_gives_up_instead_of_looping(small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ChunkMeta()
    meta.chunk_faults = ["unsent", "unsent", "unsent", "unsent"]
    out, _ = _run(meta, d)
    assert out.stop_kind == "unknown" and "unknown" in out.error
    assert "finish" not in [p for p, _ in meta.phases]


def _guard_offsets(tmp_path):
    """Смещение за концом файла или назад обязано остановить загрузку до finish."""
    for fault in ("bogus_ahead", "bogus_back"):
        d, f = _big_draft(tmp_path)
        meta = ChunkMeta()
        meta.chunk_faults = ["", fault]
        out, _ = _run(meta, d)
        assert out.stop_kind == "video_error" and "offset" in out.error, (fault, out.error)
        assert "finish" not in [p for p, _ in meta.phases]


def test_an_impossible_offset_from_meta_stops_before_finish(small, tmp_path):
    _guard_offsets(tmp_path)


# ───────────────────────── размер, бюджет вызовов ─────────────────────────

def test_the_limit_is_what_the_chunked_path_supports():
    assert cr.VIDEO_SINGLE_MAX_BYTES == 200 * MB
    assert cr.VIDEO_MAX_BYTES == wp.UPLOAD_FILE_MAX == 1536 * MB
    assert cr.VIDEO_CHUNK_MAX == wp.UPLOAD_CHUNK_MAX == 8 * MB


def test_a_file_above_the_limit_is_refused_with_a_clear_message(small, monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "VIDEO_MAX_BYTES", 2 * MB)
    d, f = _big_draft(tmp_path)
    meta = ChunkMeta()
    out, _ = _run(meta, d)
    assert out.stop_kind == "plan" and "larger than 2 MB" in out.error
    assert meta.posts == []


def test_preflight_rule_allows_big_videos_and_refuses_above_the_new_limit(small, monkeypatch, tmp_path):
    from tests.test_acl1819_preflight import AuditMeta, _audit, _by

    d, f = _big_draft(tmp_path)
    got = _by(_audit(AuditMeta(), d))
    assert got["media_limits"]["status"] == "ok"
    monkeypatch.setattr(cr, "VIDEO_MAX_BYTES", 2 * MB)
    got = _by(_audit(AuditMeta(), d))
    assert got["media_limits"]["status"] == "error" and "larger than 2 MB" in got["media_limits"]["detail"]
    assert "video 1536 MB" in (ROOT / "src/fb_api/preflight_rules.json").read_text(encoding="utf-8")


def test_chunk_calls_are_counted_per_file_size():
    # Файл ровно на пороге - один запрос, без кусков; больше - куски по 8 МБ плюс start/finish/запас.
    assert cr.upload_calls(cr.VIDEO_SINGLE_MAX_BYTES) == 0
    assert cr.upload_calls(cr.VIDEO_SINGLE_MAX_BYTES + 1) == -(-(cr.VIDEO_SINGLE_MAX_BYTES + 1) // cr.VIDEO_CHUNK_MAX) + cr.CALLS_UPLOAD_FIXED
    assert cr.upload_calls(cr.VIDEO_MAX_BYTES) == 192 + cr.CALLS_UPLOAD_FIXED


def test_a_plan_that_cannot_fit_the_call_budget_is_refused_up_front(small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ChunkMeta()
    out, _ = _run(meta, d, max_calls=25)
    assert out.stop_kind == "plan" and "need about" in out.error and "Meta calls" in out.error
    assert meta.posts == []


def test_chunks_and_the_rest_of_the_plan_share_one_budget_and_the_refusal_comes_before_writes(small, tmp_path,
                                                                                            monkeypatch):
    """ACL-1768: куски + записи ВСЕХ наборов (оценка плана) против бюджета, а не куски + фиксированный запас."""
    d, f = _big_draft(tmp_path)
    monkeypatch.setattr(cr, "CALLS_RESERVE_OTHER", 0)       # прежняя проверка (куски + запас) пропустила бы
    meta = ChunkMeta()
    chunks = cr.upload_calls(f.stat().st_size)
    rest = cr.estimate_create_calls(cr.plan_from_draft(d))
    out, _ = _run(meta, d, max_calls=chunks + rest - 1)
    assert out.stop_kind == "plan" and "rest of the campaign" in out.error and meta.posts == []


def test_the_default_budget_fits_a_typical_big_video_and_refuses_the_largest(tmp_path):
    """Бюджет считается по плану (ACL-1768): кусков и остальной кампании вместе - не больше него."""
    import tests.test_acl1768_ad_sets as T
    from src.web import publish_api

    one = T._draft(tmp_path, sets=2, ads=1)
    one.ad_sets = one.ad_sets[:1]
    plan = cr.plan_from_draft(one)
    budget, refusal = publish_api.create_call_budget(plan)
    rest = cr.estimate_create_calls(plan)
    assert refusal == ""
    # 247 МБ = 31 кусок + 3: влезает; 1,5 ГБ = 195 вызовов вместе с остальным планом - отказ заранее («разбейте»).
    assert cr.upload_calls(247 * 1024 * 1024) + rest <= budget
    assert cr.upload_calls(cr.VIDEO_MAX_BYTES) + rest > budget


# ───────────────────────── мутации: охранники обязаны падать ─────────────────────────

def test_mutation_allowing_a_wrong_phase_is_caught(monkeypatch):
    monkeypatch.setattr(wp, "UPLOAD_PHASES", wp.UPLOAD_PHASES + ("cancel",))
    monkeypatch.setitem(wp.UPLOAD_KEYS, "cancel", frozenset({"upload_phase", "upload_session_id", "file_size"}))
    with pytest.raises(BaseException) as caught:
        _guard_phase()
    assert "DID NOT RAISE" in str(caught.value) or isinstance(caught.value, (AssertionError, pytest.fail.Exception))


def test_mutation_allowing_an_oversized_chunk_is_caught(monkeypatch):
    monkeypatch.setattr(wp, "UPLOAD_CHUNK_MAX", 1 << 40)
    with pytest.raises(BaseException) as caught:
        _guard_chunk()
    assert "DID NOT RAISE" in str(caught.value) or isinstance(caught.value, (AssertionError, pytest.fail.Exception))


def test_mutation_skipping_the_offset_check_is_caught(small, monkeypatch, tmp_path):
    def lax(body, size, floor, what):
        return int(body.get("start_offset", 0)), int(body.get("end_offset", 0))

    monkeypatch.setattr(cr.CampaignCreator, "_offsets", staticmethod(lax))
    with pytest.raises(AssertionError):
        _guard_offsets(tmp_path)


# ───────────────────────── waterfall_id (17.10, Meta 1363049) ─────────────────────────

def test_the_policy_takes_a_waterfall_id_and_only_as_32_hex():
    _ok({**START, "waterfall_id": "0123456789abcdef" * 2})
    _ok({**FINISH, "waterfall_id": "f" * 32})
    _refused({**START, "waterfall_id": "F" * 32})
    _refused({**START, "waterfall_id": "x" * 32})
    _refused({**START, "waterfall_id": 123})


def test_every_phase_of_one_upload_carries_the_same_waterfall_id(small, tmp_path):
    d, _f = _big_draft(tmp_path)
    meta = ChunkMeta()
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    ids = {params.get("waterfall_id") for _path, params in meta.posts if "upload_phase" in params}
    assert len(ids) == 1 and None not in ids
    assert re.fullmatch(r"[0-9a-f]{32}", ids.pop())


# ───────────────────────── ACL-1998: шаги прогресса при долгой загрузке ─────────────────────────

def _upload_steps(out):
    return [s for s in out.steps if ": uploaded " in s]


def test_a_frozen_clock_reports_progress_about_every_ten_percent(small, tmp_path):
    # 25 кусков по 1 МБ, часы стоят: сработать может только правило «каждые 10%» (2,5 МБ -> каждый 3-й кусок).
    d, f = _big_draft(tmp_path, size=25 * MB)
    meta = ChunkMeta()
    out, _ = _run(meta, d, clock=lambda: 0.0)
    assert out.error == "", out.error
    steps = _upload_steps(out)
    assert 5 <= len(steps) <= 10 and len(steps) < 25
    done = [int(re.search(r"uploaded (\d+)/25 MB", s).group(1)) for s in steps]
    assert done == sorted(done) and done[-1] < 25
    # Итог тот же: файл цел, видео одно.
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == _sha(f)
    assert len(_video_objects(meta)) == 1


def test_slow_chunks_report_by_time_not_per_chunk(small, tmp_path, monkeypatch):
    # Файл 100 кусков: 10% = каждые 10 кусков. Часы идут по 5 с на вызов часов -> шаг по времени чаще 10%.
    d, _ = _big_draft(tmp_path, size=100 * MB)
    ticks = iter(range(0, 10_000_000, 5))
    out, _ = _run(ChunkMeta(), d, clock=lambda: float(next(ticks)), max_calls=400)
    assert out.error == "", out.error
    steps = _upload_steps(out)
    assert 10 < len(steps) < 100            # чаще 10%-правила, но не на каждый кусок


def test_progress_steps_do_not_change_the_result(small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ChunkMeta()
    out, _ = _run(meta, d)
    assert out.error == "" and out.readback["ok"] is True
    assert [o for p, o in meta.phases if p == "transfer"] == [0, MB, 2 * MB, 3 * MB]
    assert any("chunked upload started" in s for s in out.steps)
