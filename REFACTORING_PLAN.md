# Unifideck Structure Refactoring Plan

## Current Issues
1. Python backend files scattered at repository root
2. Confusing .gitignore with backend/ symlink logic
3. Build process doesn't follow standard Decky template conventions
4. defaults/backend/ exists but some backend code is still at root

## Target Structure (matching decky-plugin-template & decky-framegen)
```
unifideck/
├── .vscode/           # VS Code tasks (build.sh uses decky CLI)
├── assets/            # Plugin icons, images
├── backend/           # All Python backend code (committed)
│   ├── __init__.py
│   ├── auth/
│   ├── compat/
│   ├── discovery/
│   ├── download/
│   ├── metadata/
│   ├── registry/
│   ├── stores/
│   └── utils/
├── bin/               # Downloaded CLI tools (gitignored)
├── cli/               # Decky CLI (downloaded by setup, gitignored)
├── defaults/          # Default config files
│   └── settings.json
├── dist/              # Rollup build output (gitignored)
├── node_modules/      # npm dependencies (gitignored)
├── out/               # Decky CLI build output - final .zip (gitignored)
├── py_modules/        # Python dependencies (gitignored)
├── scripts/           # Build/utility scripts
├── src/               # Frontend TypeScript/React code
├── .gitignore
├── decky.pyi          # Decky Python types
├── justfile           # Task runner (optional)
├── main.py            # Plugin entrypoint (imports from backend/)
├── package.json
├── plugin.json
├── pnpm-lock.yaml
├── requirements.txt
├── rollup.config.mjs
└── tsconfig.json
```

## Changes Needed

### 1. Move Python files to backend/
- Move cloud_save_manager.py → backend/cloud_save.py
- Move steamgriddb_client.py → backend/steamgriddb.py  
- Move steam_user_utils.py → backend/steam_utils.py
- Move vdf_utils.py → backend/vdf.py
- Move launch_options_parser.py → backend/launch_options.py
- Keep main.py at root (entrypoint, imports from backend/)
- Merge defaults/backend/ into backend/ (remove duplication)

### 2. Update main.py imports
- Change all imports from root modules to backend.module

### 3. Clean up .gitignore
- Remove confusing backend/ symlink comments
- Keep simple: backend/ is committed, out/ is ignored
- Match template conventions

### 4. Verify build process
- Ensure .vscode/build.sh uses decky CLI correctly
- Ensure out/ directory contains proper plugin structure

### 5. Update documentation
- Update README with new structure
- Update CONTRIBUTING.md if needed
