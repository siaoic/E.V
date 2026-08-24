import socketio
import sys
import threading

# 创建一个标准的 Socket.IO 客户端
sio = socketio.Client()


# --- 事件处理器 ---

@sio.event
def connect():
    """
    连接成功时的回调函数
    """
    print('已连接到Mindcraft服务器')
    sio.emit('listen-to-agents')
    print('现在可以输入消息与机器人聊天了！')
    print('输入消息 > ', end='', flush=True)


@sio.event
def connect_error(data):
    """
    连接错误时的回调函数
    """
    print('连接失败:', data)


@sio.on('bot-output')
def on_bot_output(agent_name, message):
    """
    接收到机器人回复时的回调函数
    """
    # 清除当前行并打印消息，然后再重新显示提示符
    print(f'\n[{agent_name}] 回复: {message}')
    print('输入消息 > ', end='', flush=True)


@sio.event
def disconnect():
    """
    断开连接时的回调函数
    """
    print('已断开连接')


# --- 消息发送与主逻辑 ---

def send_message(agent_name, message):
    """
    向指定的机器人发送消息
    """
    sio.emit('send-message', (agent_name, {
        'from': 'ADMIN',
        'message': message
    }))
    print(f'你: {message}')


def input_loop():
    """
    处理用户输入的循环
    """
    print('正在连接到Mindcraft服务器...')
    print('命令: quit=退出, !stop=停止动作, !stay=保持静止')

    while True:
        try:
            # 从标准输入读取一行
            message = sys.stdin.readline().strip()
            if not message:
                break

            if message.lower() in ['quit', 'q']:
                break
            elif message:
                send_message('fake-neuro', message)

            # 重新显示提示符
            if sio.connected:
                print('输入消息 > ', end='', flush=True)

        except KeyboardInterrupt:
            # 捕捉 Ctrl+C
            break

    # 循环结束后断开连接
    if sio.connected:
        print('\n正在断开连接...')
        sio.disconnect()


if __name__ == '__main__':
    try:
        # 连接到服务器
        sio.connect('http://localhost:8080')

        # 在一个独立的线程中运行输入循环，以避免阻塞Socket.IO事件处理
        input_thread = threading.Thread(target=input_loop)
        input_thread.daemon = True
        input_thread.start()

        # 等待Socket.IO客户端断开连接
        sio.wait()

    except socketio.exceptions.ConnectionError as e:
        print(f"连接错误: {e}")
    except Exception as e:
        print(f"发生未知错误: {e}")
    finally:
        print("程序退出。")