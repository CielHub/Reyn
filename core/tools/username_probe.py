"""
Modul: core/tools/username_probe.py
Tanggung Jawab: ALAT DIAGNOSTIK MANUAL (BUKAN bagian flow utama Phase 1-8,
tidak dipanggil dari mana pun secara otomatis) -- dijalankan SENDIRI oleh
operator lewat Termux untuk mencari tahu file/key internal Roblox mana yang
BENERAN menyimpan username akun yang sedang login di tiap package, SEBELUM
logic scan otomatis (Phase 4.5) dibuat.

KENAPA ALAT INI ADA:
Tidak ada dokumentasi publik yang reliable soal struktur file internal
Roblox Android (beda APK/versi/build bisa beda). Daripada menebak path/key
lalu bikin parser yang keliatan lengkap tapi sebetulnya salah/rapuh, alat
ini cuma MEMBACA (read-only, tidak pernah menulis apa pun) lalu menampilkan
apa yang BENERAN ada di device -- supaya parser resmi Phase 4.5 dibuat
berdasarkan bukti, bukan tebakan.

CARA PAKAI:
  cd ke folder root CARRERA-HUB, lalu:
    python3 -m core.tools.username_probe
  Copy-paste SELURUH output-nya (dari baris "=== USERNAME PROBE" sampai
  baris "SELESAI") balik ke chat Claude.

KEAMANAN (WAJIB DIJAGA, JANGAN DILONGGARKAN):
- HANYA baca (list directory + cat file teks) lewat 'su -c' -- TIDAK PERNAH
  menulis, menghapus, atau mengubah apa pun di device.
- Key yang namanya mengindikasikan credential sensitif (password, token,
  cookie, secret, auth, session, roblosecurity, refresh, jwt, bearer, hash,
  credential) TIDAK PERNAH ditampilkan value aslinya -- cuma nama key +
  "[REDACTED]".
- Value yang panjangnya kelewat panjang (>80 karakter -- ciri khas token/
  cookie/session id) ikut di-redact otomatis WALAUPUN nama key-nya tidak
  cocok pola di atas (jaga-jaga untuk key dengan nama tidak jelas).
- File database SQLite ('databases/') CUMA di-list nama filenya di sini,
  isinya TIDAK dibaca (butuh parser SQLite terpisah kalau ternyata memang
  di situ sumbernya -- nunggu hasil probe ini dulu).
"""
import re
import subprocess

from core.scanner import get_roblox_packages

_SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|token|cookie|secret|auth|session|roblosecurity|"
    r"refresh|jwt|bearer|hash|credential|security)",
    re.IGNORECASE,
)
_MAX_SAFE_VALUE_LENGTH = 80


def _run_su(cmd: str) -> str:
    try:
        result = subprocess.run(
            ["su", "-c", cmd], capture_output=True, text=True, timeout=5, errors="replace"
        )
        return (result.stdout or "").strip()
    except Exception as e:
        return f"[ERROR: {e}]"


def _redact_if_sensitive(key: str, value: str) -> str:
    if _SENSITIVE_KEY_PATTERN.search(key):
        return "[REDACTED - nama key sensitif]"
    if len(value) > _MAX_SAFE_VALUE_LENGTH:
        return f"[REDACTED - value {len(value)} karakter, kemungkinan token/cookie]"
    return value if value else "(kosong)"


def _probe_package(pkg: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"PACKAGE: {pkg}")
    print(f"{'=' * 70}")

    base = f"/data/data/{pkg}"

    print("\n-- shared_prefs/ (daftar file) --")
    print(_run_su(f"ls -la {base}/shared_prefs/ 2>/dev/null") or "(kosong / tidak bisa diakses)")

    print("\n-- databases/ (daftar file SAJA, isi TIDAK dibaca di sini) --")
    print(_run_su(f"ls -la {base}/databases/ 2>/dev/null") or "(kosong / tidak bisa diakses)")

    print("\n-- files/ (top-level saja) --")
    print(_run_su(f"ls -la {base}/files/ 2>/dev/null") or "(kosong / tidak bisa diakses)")

    xml_files_raw = _run_su(f"ls {base}/shared_prefs/*.xml 2>/dev/null")
    xml_files = [f.strip() for f in xml_files_raw.splitlines() if f.strip()]
    for xml_path in xml_files:
        print(f"\n-- isi {xml_path} (key sensitif & value panjang di-redact) --")
        content = _run_su(f"cat '{xml_path}' 2>/dev/null")
        if not content:
            print("(tidak bisa dibaca)")
            continue
        # Regex sederhana, BUKAN parser XML penuh -- sengaja, supaya tetap
        # jalan meski ada file yang agak malformed. Cocok utk 2 pola umum
        # shared_prefs Android: <string name="K">V</string> dan
        # <TAG name="K" value="V" />
        matches = list(re.finditer(r'name="([^"]+)"[^>]*(?:value="([^"]*)"|>([^<]*)<)', content))
        if not matches:
            print("  (tidak ada pasangan key-value yang cocok pola umum shared_prefs)")
            continue
        for m in matches:
            key = m.group(1)
            value = m.group(2) if m.group(2) is not None else (m.group(3) or "")
            print(f"  {key} = {_redact_if_sensitive(key, value)}")


def main():
    print("=== USERNAME PROBE (diagnostik manual, BUKAN bagian flow utama) ===")
    print("Tool ini CUMA baca (read-only) shared_prefs/databases/files tiap")
    print("package Roblox buat cari tau field mana yang nyimpen username akun")
    print("yang lagi login. Key yang keliatan sensitif (password/token/cookie/")
    print("session/dll) otomatis di-redact -- value aslinya TIDAK ditampilkan.")
    print()
    print("Setelah selesai, copy-paste SELURUH output ini balik ke chat Claude")
    print("supaya parser resmi Phase 4.5 bisa dibuat berdasarkan struktur yang")
    print("BENERAN ada di device kamu.\n")

    packages = get_roblox_packages()
    if not packages:
        print("Tidak ada package Roblox terdeteksi.")
        return

    print(f"Package Roblox terdeteksi ({len(packages)}): {', '.join(packages)}")
    for pkg in packages:
        _probe_package(pkg)

    print(f"\n{'=' * 70}")
    print("SELESAI. Copy-paste semua output di atas (dari '=== USERNAME PROBE'")
    print("sampai baris ini) balik ke chat Claude.")


if __name__ == "__main__":
    main()
