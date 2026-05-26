def mcp_query(query: str, mcp_router) -> None : 
    if query.strip() == "/mcp":
        # 显示帮助信息
        print("MCP Commands:")
        print("  /mcp list              - List all connected MCP servers")
        print("  /mcp show <server>     - Show detailed tools of a specific server")
        print("  /mcp tool <server> <tool> - Show detailed schema of a specific tool")
        print("  /mcp search <keyword>  - Search tools by keyword")
        print()
        
    elif query.strip().startswith("/mcp list"):
        # 列出所有服务器和工具数量
        if mcp_router.clients:
            print("\nConnected MCP Servers:")
            for name, c in mcp_router.clients.items():
                tools = c.get_agent_tools()
                print(f"  📦 {name}: {len(tools)} tools")
                # 可选：显示工具名称预览
                for t in tools[:3]:  # 只显示前3个
                    print(f"    - {t['name']}")
                if len(tools) > 3:
                    print(f"    ... and {len(tools) - 3} more")
            print()
        else:
            print("  (no MCP servers connected)")
            
    elif query.strip().startswith("/mcp show "):
        # 显示特定服务器的所有工具详情
        server_name = query.strip().split(" ", 2)[2]
        if mcp_router.clients and server_name in mcp_router.clients:
            client = mcp_router.clients[server_name]
            tools = client.get_agent_tools()
            print(f"\n🔧 {server_name} ({len(tools)} tools):")
            for tool in tools:
                print(f"\n  📌 {tool['name']}")
                print(f"     Description: {tool.get('description', 'No description')}")
                print(f"     Server tool: {tool.get('_mcp_tool')}")
                # 简洁显示参数
                params = tool.get('input_schema', {}).get('properties', {})
                required = tool.get('input_schema', {}).get('required', [])
                if params:
                    print(f"     Parameters:")
                    for param_name, param_info in params.items():
                        required_mark = "✓" if param_name in required else " "
                        param_type = param_info.get('type', 'any')
                        print(f"       [{required_mark}] {param_name}: {param_type}")
                        if 'description' in param_info:
                            print(f"           {param_info['description']}")
        else:
            print(f"  Server '{server_name}' not found")
            
    elif query.strip().startswith("/mcp tool "):
        # 显示特定工具的完整 schema
        parts = query.strip().split(" ", 3)
        if len(parts) >= 4:
            _, _, server_name, tool_name = parts
            if mcp_router.clients and server_name in mcp_router.clients:
                client = mcp_router.clients[server_name]
                tools = client.get_agent_tools()
                target_tool = next((t for t in tools if t['name'] == tool_name), None)
                if target_tool:
                    print(f"\n🔍 Detailed info for {tool_name}:")
                    print(f"  Server: {target_tool.get('_mcp_server')}")
                    print(f"  Original tool: {target_tool.get('_mcp_tool')}")
                    print(f"  Description: {target_tool.get('description', 'No description')}")
                    print(f"  Input Schema:")
                    import json
                    print(json.dumps(target_tool.get('input_schema', {}), indent=4))
                else:
                    print(f"  Tool '{tool_name}' not found on server '{server_name}'")
            else:
                print(f"  Server '{server_name}' not found")
        else:
            print("  Usage: /mcp tool <server> <tool_name>")
            
    elif query.strip().startswith("/mcp search "):
        # 搜索工具
        keyword = query.strip().split(" ", 2)[2].lower()
        print(f"\n🔎 Searching for tools containing '{keyword}':")
        found = False
        for server_name, client in mcp_router.clients.items():
            tools = client.get_agent_tools()
            matches = [t for t in tools if keyword in t['name'].lower() or keyword in t.get('description', '').lower()]
            if matches:
                found = True
                print(f"\n  📦 {server_name}:")
                for tool in matches:
                    print(f"    - {tool['name']}")
                    if keyword in tool.get('description', '').lower():
                        print(f"      {tool['description'][:100]}...")
        if not found:
            print("  No tools found")