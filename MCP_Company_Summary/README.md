# MCP Company Summary

Generate a one-page PDF company profile using Moody's Credit MCP.

## Requirements

- Python 3.10 or newer
- Access to Moody's Credit MCP
- A VS Code MCP configuration containing an `MCP-Credit` server with its URL
  and `Authorization` header

Install the Python packages:

```bash
python3 -m pip install -r requirements.txt
```

By default, the program reads `MCP-Credit` from VS Code's user MCP
configuration at `~/Library/Application Support/Code/User/mcp.json`. To use a
different config file, set `MOODYS_MCP_CONFIG` to its path. Alternatively, set
both `MOODYS_MCP_URL` and `MOODYS_MCP_AUTHORIZATION` in the environment to
override the values read from the config.

Keep the MCP configuration and authorization token private. Do not commit
credentials to this repository. Rotate any token that has been exposed.

Run the program:

```bash
python3 company_summary.py
```

Enter a company name and select the matching entity if several are found. The
generated PDF is saved next to the Python program and is excluded from Git.
