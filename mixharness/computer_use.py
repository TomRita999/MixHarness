from pydantic import BaseModel, ConfigDict, Field, model_validator


DESKTOP_TARGET = 'desktop'


class ReadWindow(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    share: str = Field(min_length=32, max_length=32, description='用户在当前会话授予的共享编号。')


class ComputerTarget(ReadWindow):
    target: str = Field(max_length=96, pattern='^(display:[0-9]+|[0-9]+-[0-9]+-[0-9]+)$',
                        description='本次 computer_list 返回的显示器或窗口 target。')


class ComputerFrame(ComputerTarget):
    frame: str = Field(min_length=32, max_length=32, description='本任务最近一次截取该目标时返回的画面编号。')


class ComputerPoint(ComputerFrame):
    x: int = Field(ge=0, description='截图内的像素横坐标，原点在左上角。')
    y: int = Field(ge=0, description='截图内的像素纵坐标，原点在左上角。')


class ComputerDrag(ComputerPoint):
    to_x: int = Field(ge=0, description='同一截图内拖拽终点的像素横坐标。')
    to_y: int = Field(ge=0, description='同一截图内拖拽终点的像素纵坐标。')


class ComputerScroll(ComputerPoint):
    delta_x: int = Field(default=0, ge=-2000, le=2000, description='水平滚动像素数，正值向右。')
    delta_y: int = Field(default=0, ge=-2000, le=2000, description='垂直滚动像素数，正值向下。')

    @model_validator(mode='after')
    def check_distance(self):
        if self.delta_x == 0 and self.delta_y == 0:
            raise ValueError('请指定水平或垂直滚动距离。')
        return self


class ComputerText(ComputerFrame):
    text: str = Field(min_length=1, max_length=4096)


class ComputerKey(ComputerFrame):
    key: str = Field(min_length=1, max_length=64, description='单键或快捷键，例如 Tab、Cmd+A、Cmd+Shift+Z。')


class ComputerWait(ReadWindow):
    milliseconds: int = Field(ge=1, le=10000, description='等待界面更新的毫秒数，等待结束后可重新读取画面。')


COMPUTER_TOOLS = {
    'computer_list': (ReadWindow, '列出桌面', '列出当前显示器、可见应用及窗口，包含窗口所属应用和可用的目标编号。'),
    'computer_capture': (ComputerTarget, '读取画面', '读取一整块显示器或指定窗口的截图，返回画面编号和像素尺寸。'),
    'computer_inspect': (ComputerTarget, '读取界面控件', '读取指定窗口的辅助功能树。选择显示器时读取该显示器上的焦点窗口。'),
    'computer_wait': (ComputerWait, '等待界面更新', '等待指定时间，随后可以重新截图或读取控件。'),
    'computer_focus': (ComputerFrame, '切换到窗口', '激活指定窗口所属的应用并将窗口置于前方。操作后重新截图。'),
    'computer_click': (ComputerPoint, '点击', '在最近一次截图的指定位置单击鼠标左键。'),
    'computer_double_click': (ComputerPoint, '双击', '在最近一次截图的指定位置双击鼠标左键。'),
    'computer_drag': (ComputerDrag, '拖拽', '在同一截图中按住鼠标左键，从起点拖到终点。'),
    'computer_type': (ComputerText, '输入文字', '向目标窗口或目标显示器上的焦点控件输入文字。'),
    'computer_scroll': (ComputerScroll, '滚动', '在截图指定位置按给定的像素距离滚动。'),
    'computer_key': (ComputerKey, '执行快捷键', '向目标窗口或目标显示器上的焦点控件发送按键或快捷键。'),
}
COMPUTER_READS = ('computer_list', 'computer_capture', 'computer_inspect', 'computer_wait')


def target_names(listing):
    names = {}
    for display in listing['displays']:
        caption = '主显示器' if display['main'] else '显示器'
        names[display['target']] = caption + ' · ' + display['target']
    for window in listing['windows']:
        names[window['target']] = ' · '.join(filter(None, (window['app'], window['title']))) or window['target']
    return names
