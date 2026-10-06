import os
import re
import subprocess


_PID_RE = re.compile(r"^\d+$")


def _read_exact_pid():
    while True:
        pid = input("Masukkan PID Roblox target yang sedang diuji: ").strip()
        if _PID_RE.fullmatch(pid):
            return pid
        print("[!] PID tidak valid. Masukkan angka PID saja.")


def main():
    print("=== CARRERA LOG CATCHER (PID-SCOPED) ===")
    print("[1] Tidak ada logcat -c. Buffer global Android tidak disentuh.")

    pid = _read_exact_pid()

    print(
        "\n[2] STANDBY. Lakukan testing pada clone target "
        f"(PID {pid})."
    )
    input(
        "\n>>> TEKAN ENTER SETELAH PESAN KICK/ERROR MUNCUL "
        "DI CLONE TARGET <<<"
    )

    print("\n[3] Menyimpan log PID target...")
    os.makedirs("logs", exist_ok=True)
    log_path = os.path.join("logs", f"kick_evidence_{pid}.txt")

    result = subprocess.run(
        [
            "su",
            "-c",
            f"logcat --pid={pid} -d -v threadtime",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )

    if result.returncode != 0:
        print(
            "[!] Gagal mengambil log PID target: "
            f"{(result.stderr or '').strip()}"
        )
        return 1

    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(result.stdout or "")

    print(f"\n[V] SELESAI! Bukti PID {pid} disimpan ke: {log_path}")
    print(
        "Buka file tersebut lalu cari 'disconnect', 'kicked', "
        "atau error code yang relevan."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
