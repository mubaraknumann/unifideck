# Contributing to Unifideck

Thank you for your interest in contributing to Unifideck! We welcome contributions from the community to help improve this project. To ensure a smooth collaboration process, please review the following guidelines before you start.

## Core Contribution Rules

To maintain the quality and direction of the project, please adhere to these mandatory rules:

1.  **Discuss Before You Start**: All new contributions must be discussed beforehand via Discord (https://discord.gg/H8aTyGH9) or Email. Do not start working on a significant change without prior communication.
2.  **Approval Required**: New features and Pull Requests (PRs) must be approved and integrated into the implementation plan/roadmap _before_ you submit a PR. Unsolicited PRs for major features may be rejected if they haven't been planned for.
3.  **Follow Existing Style**: You must follow the existing programming style and architecture of the codebase. Consistency is key.
4.  **No Sweeping Changes**: We cannot accept sweeping changes that refactor large portions of the codebase at once. Please keep changes focused and modular.

## Getting Started

1.  **Fork the Repository**: Create a fork of the repository to work on your changes.
2.  **Clone the Repository**: Clone your fork locally.
3.  **Create a Branch**: Create a new branch for your specific feature or fix.
    ```bash
    git checkout -b feature/your-feature-name
    ```

## Pull Request Process

1.  Ensure your code adheres to the project's coding standards.
2.  Create a Pull Request with details of changes to the interface, this includes new environment variables, exposed ports, useful file locations, and container parameters.
3.  Increase the version numbers in any examples files and the README.md to the new version that this Pull Request would represent.
4.  Always provide a working build of the plugin. This is required so we can test the build and check for regressions.
5.  As of right now, **mubaraknumann** has final say on what gets merged into the main branch.

## Bug Reports

We use GitHub issues to track public bugs. Report a bug by [opening a new issue](); it's that easy!

**Great Bug Reports** tend to have:

- A quick summary and/or background.
- Steps to reproduce.
- What you expected would happen.
- What actually happened.
- Notes (possibly including why you think this might be happening, or stuff you tried that didn't work).

## Code Style

- **Python**: We follow standard Python PEP 8 style guides.
- **TypeScript/React**: Please use the existing ESLint and Prettier configurations if available.
- **Comments**: Document your code where necessary, especially for complex logic.

## Behavior

Please be respectful and considerate of others when contributing. Please do not assume that we are going to accept your Pull Request. We reserve the right to reject any Pull Request that does not meet our standards.
