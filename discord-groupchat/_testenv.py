"""テストが【本物の記録】を汚さないようにする隔離。

事故（2026-08-21）：テストを1回流すたびに、本物の
  history/errors.log     …偽のエラーが1件増える
  history/trace.jsonl    …偽の「発言がどの機能に流れたか」が増える
  history/last_gen.json  …偽の「直前の生成」が書かれる
  history/gen_settings.json …モデル設定が書き換わる
に書き込まれていた。ボットは再起動のたびに自己テストを流すので、
再起動のたびに偽のエラーが注入されていたことになる。

実害：デバッグログの「直近のエラー」が偽物で埋まり、開発側がそれを見て
存在しない不具合を何時間も追いかけた（08-16〜08-21 に何度も）。
累積254件の「プロンプトの英訳」エラーは、ほぼ全部これだった。

対策：テストの読み込み直後にここを呼び、書き込み先を全部
一時ディレクトリへ向ける。テスト側の作法（毎回退避して戻す）に
頼らない＝忘れても汚れない。
"""
import tempfile
from pathlib import Path

# HISTORY_DIR の下に作られる書き込み先。増えたらここに足す
# （足し忘れても _assert_clean が実行後に気づかせる）。
_PATHS = (
    "ERROR_LOG", "TRACE_FILE", "TASK_TIMES_FILE", "GEN_SETTINGS_FILE",
    "MYCH_FILE", "CLIP_DIR", "WHISPER_DIR", "_MOTION_JOB_FILE",
    "_LASTGEN_FILE", "DESIGN_DIR", "STYLE_PROFILE_FILE", "SHORTS_LOG",
    "RESTART_MARKER", "SELF_BACKUP_DIR",
)


def isolate(bot):
    """bot の書き込み先を一時ディレクトリへ移す。戻り値は そのディレクトリ。"""
    tmp = Path(tempfile.mkdtemp(prefix="agc_test_"))
    real = Path(getattr(bot, "HISTORY_DIR", tmp))
    bot.HISTORY_DIR = tmp
    for name in _PATHS:
        cur = getattr(bot, name, None)
        if cur is None:
            continue
        cur = Path(cur)
        try:                       # 本物の history/ の下にあるものだけ移す
            rel = cur.relative_to(real)
        except ValueError:
            continue
        setattr(bot, name, tmp / rel)
    (tmp).mkdir(parents=True, exist_ok=True)
    return tmp, real


def assert_clean(real_dir, before):
    """テストが本物の記録を触っていないことを確かめる。
    触っていたら、隔離し忘れた書き込み先があるということ。"""
    now = _snapshot(real_dir)
    changed = sorted(k for k, v in now.items() if before.get(k) != v)
    return changed


def _snapshot(d):
    d = Path(d)
    out = {}
    if not d.exists():
        return out
    for p in d.rglob("*"):
        if p.is_file():
            try:
                out[str(p)] = (p.stat().st_mtime_ns, p.stat().st_size)
            except OSError:
                pass
    return out


def snapshot(d):
    return _snapshot(d)
