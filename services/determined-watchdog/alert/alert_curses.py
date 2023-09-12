import curses
import pyfiglet


def main(stdscr):
    # 初始化ncurses
    curses.curs_set(0)  # 隐藏光标
    stdscr.clear()

    # 获取终端窗口的大小
    height, width = stdscr.getmaxyx()

    # 显示运行时间
    large_text = pyfiglet.figlet_format("Lins Lab", font="univers")

    stdscr.addstr(4, 1, large_text, curses.A_BOLD)

    stdscr.addstr(1, 1, "运行时间: 2小时 30分钟", curses.A_BOLD)
    stdscr.addstr(2, 1, "距离上次token 更新时间: 7天前", curses.A_BOLD)

    # 定义颜色
    curses.start_color()
    curses.init_pair(1, curses.COLOR_YELLOW, curses.COLOR_BLACK)  # 黄色前景，黑色背景

    # 设置属性
    author_text = "author : Yufan Wang 2023.05 - 2023.09"
    stdscr.addstr(
        height - 3,
        1,
        author_text,
        curses.A_BOLD | curses.A_UNDERLINE | curses.color_pair(1),
    )

    # 显示作者信息
    stdscr.addstr(height - 3, 1, "author : Yufan Wang 2023.05 - 2023.09", curses.A_BOLD)
    stdscr.addstr(height - 2, 1, "press ctrl + c  to exit ", curses.A_BOLD)
    stdscr.refresh()

    # 等待用户输入并退出
    while True:
        key = stdscr.getch()
        if key == ord("q"):
            break


def generate_large_text(text):
    result = pyfiglet.figlet_format(text, font="univers")
    return result


if __name__ == "__main__":
    curses.wrapper(main)
