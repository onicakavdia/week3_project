What built?
1. It manages conversational history by saving and loading chat sessions as structured JSON files.
2. LLMs get outdated quickly and struggle with hyper-specific details or local project files.
3. By designing read_file, write_file, and a targeted edit_file function , you built a system capable of modifying project codebases or generating text documents autonomously.

Why built?
1. Wanted a workflow where research context isn't lost when the program closes. The agent saves state automatically and lets you jump back into previous tasks using /resume <session_id>.

2. Resolve_path: Letting an AI rewrite or read any path on a computer is dangerous. You explicitly implemented path validation to guarantee the agent can never use relative jumps (like ../../etc/passwd) to escape the assigned workspace.


It isn't just a simple chatbotWrapper; it is a dynamic agent equipped with specific tools to bridge the gap between static model training and real-time knowledge.
