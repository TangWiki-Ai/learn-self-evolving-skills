@AGENTS.md

# Claude Code entrypoint

When the user provides this repository URL and asks you to pull it or install
dependencies:

1. Clone `https://github.com/TangWiki-Ai/learn-self-evolving-skills` into
   `learn-self-evolving-skills` if it is absent. If that directory exists,
   inspect it first and preserve its files.
2. Enter the repository and run `uv sync --no-dev --locked`.
3. Read `README.md` and
   `.agents/skills/self-evolving-skill-instructor/SKILL.md`.
4. Follow the instructor Skill's `New-user handoff` exactly.

The handoff introduces the project and asks whether the user wants to start
Skill self-evolution. If the user already says “我要学习 Skill 自进化” in an
installed repository, treat that as the confirmation. Do not ask for an API key
or start a paid live run before that confirmation.
