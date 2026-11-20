# clab_mcp

Basic MCP implementation to interact with the containerlab api

This is just for me to learn about MCP, and to tailer how to interact with 
APIs etc from an agent.

There are much more polished versions available such as this:

https://github.com/seanerama/clab-mcp-server


This is mostly just a backup for my own referencce.

# set up the venv:
'''
sudo python3 -m venv /opt/clab-mcp-server/.venv
sudo --preserve-env=http_proxy,https_proxy,HTTP_PROXY,HTTPS_PROXY \
  /opt/clab-mcp-server/.venv/bin/pip install -r /opt/clab-mcp-server/requirements.txt
sudo chmod -R u=rwX,go=rX /opt/clab-mcp-server
sudo systemctl restart clab-mcp
journalctl -u clab-mcp -n 20
'''
