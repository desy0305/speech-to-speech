# Office Agent Workspace

Place only the documents that the optional local Office agent may access in this directory.
The container receives no home-directory or Docker-socket mount, and paths outside this directory are rejected.

Supported inputs are Office documents, CSV, JSON, and common image formats. Mutations are limited to `.docx`, `.xlsx`, and `.pptx` files and require one-time approval in the web UI.
