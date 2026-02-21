# plugins/mcp_client_test.py
import asyncio
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run_mcp_client():
    print("正在启动 MCP 客户端...")

    # 1. 配置如何启动服务端
    # 这里我们使用当前运行该脚本的 Python 解释器去启动 bio_server.py
    server_params = StdioServerParameters(
        command=sys.executable,  # 自动获取当前的 python.exe 或 python 路径
        args=["plugins/bio_server.py"],  # 指向我们刚才写的服务端脚本
    )

    # 2. 通过标准输入输出 (stdio) 连接服务端
    print("正在尝试与 Bio-Plugin 建立连接...")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # 必须先初始化 session
            await session.initialize()
            print("✅ 连接成功！\n")

            # 3. 获取服务端提供了哪些工具
            tools_response = await session.list_tools()
            print("发现服务端提供的工具：")
            for tool in tools_response.tools:
                print(f" - {tool.name}: {tool.description}")
            print("-" * 30)

            # 4. 尝试调用 calculate_pcr_tm 工具
            test_sequence = "ATGCGTACGTTAGCTAGC"
            print(f"尝试调用 calculate_pcr_tm，测试序列: {test_sequence}")

            # call_tool 的 arguments 必须是字典
            result = await session.call_tool(
                "calculate_pcr_tm",
                arguments={"sequence": test_sequence}
            )

            # 打印服务端的返回结果
            print(f"💡 工具执行结果:\n{result.content[0].text}\n")

            # 5. 再测试一下本地基因检索
            test_gene = "GhChr01"
            print(f"尝试调用 search_gene_local，查询基因: {test_gene}")
            result2 = await session.call_tool(
                "search_gene_local",
                arguments={"gene_id": test_gene}
            )
            print(f"💡 工具执行结果:\n{result2.content[0].text}\n")


if __name__ == "__main__":
    # 运行异步客户端
    asyncio.run(run_mcp_client())