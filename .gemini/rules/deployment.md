# Deployment Target: Homelab Linux VM Only

## CRITICAL INVARIANT: ALWAYS DEPLOY TO HOMELAB LINUX VM, NEVER WINDOWS

1. **Production Host**:
   - **Homelab Server**: Linux QEMU-VM, reachable on the LAN and over Tailscale as `<homelab-host>`.
   - **SSH User**: `<ssh-user>@<homelab-host>`
   - **Remote Directory**: `~/fitness-tracker/`
   - **Production URL**: `https://<homelab-host>/` (ports 80/443 via Nginx proxy container, backend in Docker container `fitness-tracker`).
   - Concrete values for these placeholders live in the gitignored `deployment.local.md` next to this file.

2. **Windows Machine Role**:
   - The Windows workstation is strictly a **development, testing, and GPU worker environment** (provides Ollama GPU inference for the homelab container via an `extra_hosts` entry pointing `gpu-worker` at the workstation).
   - **DO NOT** deploy user updates to the local Windows machine or report the Windows scheduled task (`FitnessTracker`) as the production deployment. The user accesses the app exclusively from their phones and mobile devices via `https://<homelab-host>/`.

3. **Standard Deployment Steps**:
   Whenever deploying code changes:
   ```bash
   # 1. Sync updated code and static assets
   scp -r app static requirements.txt <ssh-user>@<homelab-host>:~/fitness-tracker/

   # 2. Set file permissions, rebuild container, and restart tracker service
   ssh <ssh-user>@<homelab-host> "chmod -R a+rX ~/fitness-tracker/app ~/fitness-tracker/static && cd ~/fitness-tracker && docker compose build tracker && docker compose up -d tracker"
   ```
