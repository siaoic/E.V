# mindcraft_chat_pro.py

import socketio
import sys
import threading

# 从你的 asr.py 文件中导入核心的 VoiceToText 类
from asr import VoiceToText

# --- 全局客户端和事件 ---

# 创建一个标准的 Socket.IO 客户端
sio = socketio.Client()
# 创建一个事件，用于在程序结束时安全地停止输入线程
stop_thread = threading.Event()


# --- Socket.IO 事件处理器 ---

@sio.event
def connect():
    """连接成功时的回调函数"""
    print('✅ 已连接到Mindcraft服务器!')
    sio.emit('listen-to-agents')


@sio.event
def connect_error(data):
    """连接错误时的回调函数"""
    print(f'❌ 连接失败: {data}')


@sio.on('bot-output')
def on_bot_output(agent_name, message):
    """接收到机器人回复时的回调函数"""
    # 清除可能存在的当前输入行，打印消息，然后重新显示提示符
    # 这样可以防止服务器消息和用户输入混在一起
    sys.stdout.write('\r' + ' ' * 80 + '\r')
    print(f"🤖 [{agent_name}] 回复: {message}")
    sys.stdout.flush()


@sio.event
def disconnect():
    """断开连接时的回调函数"""
    print('🔌 已从服务器断开连接。')
    # 发送信号以停止输入线程，防止在断线后线程继续运行
    stop_thread.set()


# --- 核心逻辑 ---

def send_message(agent_name, message):
    """向指定的机器人发送消息"""
    payload = {'from': 'ADMIN', 'message': message}
    sio.emit('send-message', (agent_name, payload))
    print(f"👤 你 -> [{agent_name}]: {message}")


def user_input_loop(asr_client: VoiceToText):
    """
    处理用户语音和文本输入的循环。
    这个函数在独立的线程中运行。
    """
    agent_name = "fake-neuro"
    use_text_input = False  # 默认使用语音输入

    print("\n--- Mindcraft 语音聊天客户端 ---")
    print(f"🤖 默认机器人: '{agent_name}'")
    print("🎤 说 'text'     -> 切换到文本模式")
    print("⌨️  输入 'voice'   -> 切换回语音模式")
    print("🤖 说/输入 'change' -> 更换机器人")
    print("🚪 说/输入 'exit'   -> 退出程序")
    print("-" * 35)

    while not stop_thread.is_set():
        try:
            if use_text_input:
                # 文本输入模式
                prompt = f"⌨️  输入消息 ({agent_name}) > "
                message = input(prompt)
            else:
                # 语音输入模式
                print(f"\n🎤 请对机器人 '{agent_name}' 说话...")
                # 直接调用asr实例的get_text方法，而不是全局函数
                message = asr_client.get_text()  # 等待语音识别结果

                if message:
                    print(f"👂 语音识别结果: '{message}'")
                else:
                    # 如果 get_text 返回 None 或空字符串，说明识别失败或超时
                    print("🔇 未能识别语音，请重试。")
                    continue

            # 处理命令 (统一处理语音和文本的输入)
            cmd = message.strip().lower()

            if cmd in ['exit', 'q']:
                print("👋 正在退出...")
                break

            if cmd == 'text':
                use_text_input = True
                print("✅ 已切换到文本输入模式。")
                continue

            if cmd == 'voice':
                use_text_input = False
                print("✅ 已切换到语音输入模式。")
                continue

            if cmd == 'change':
                new_agent = input("👤 请输入新的机器人名称: ").strip()
                if new_agent:
                    agent_name = new_agent
                    print(f"✅ 机器人已切换为: '{agent_name}'")
                else:
                    print("❌ 名称无效，操作已取消。")
                continue

            # 发送非空消息
            if message:
                send_message(agent_name, message)

        except (KeyboardInterrupt, EOFError):
            print("\n🚨 检测到中断，正在退出...")
            break

    # 循环结束后，确保主程序知道要断开连接
    if sio.connected:
        sio.disconnect()


if __name__ == '__main__':
    server_url = "http://localhost:8080"

    # 1. 初始化 ASR 客户端 (只执行一次)
    print("🚀 正在初始化语音识别服务...")
    asr_instance = VoiceToText()

    try:
        # 2. 启动 ASR 服务 (开始监听麦克风)
        asr_instance.start()

        # 3. 连接到 Socket.IO 服务器
        sio.connect(server_url, wait_timeout=10)

        # 4. 在一个独立的线程中运行输入循环，并将asr实例传递进去
        input_thread = threading.Thread(target=user_input_loop, args=(asr_instance,))
        input_thread.start()

        # 等待输入线程结束（用户输入exit或按Ctrl+C时）
        input_thread.join()

    except socketio.exceptions.ConnectionError:
        print(f"❌ 无法连接到服务器 {server_url}。请确保服务器正在运行。")
    except Exception as e:
        print(f"💥 发生未知错误: {e}")
    finally:
        # 5. 停止 ASR 服务并清理资源 (无论如何都执行)
        print("🛑 正在停止语音识别服务...")
        asr_instance.stop()
        if sio.connected:
            sio.disconnect()
        print("✅ 程序已安全关闭。")