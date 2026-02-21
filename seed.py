"""One-time seed: copy bundled data files to the persistent volume if empty."""

import os
import shutil

DATA_DIR = os.getenv("DATA_DIR", "/data")
SEED_DIR = "/app/seed_data"


def seed():
    if not os.path.isdir(SEED_DIR):
        print("No seed_data directory found, skipping seed.")
        return

    for item in os.listdir(SEED_DIR):
        src = os.path.join(SEED_DIR, item)
        dst = os.path.join(DATA_DIR, item)
        if os.path.exists(dst):
            print(f"  Skipping {item} (already exists)")
            continue
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        print(f"  Seeded {item}")


if __name__ == "__main__":
    print(f"Seeding {DATA_DIR} from {SEED_DIR}...")
    seed()
    print("Seed complete.")
