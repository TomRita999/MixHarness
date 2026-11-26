import os
import unittest

from tests.test_window_actions import WindowTargetCase


class ComputerActionTests(WindowTargetCase):
    async def test_discover_capture_and_inspect_the_test_window(self):
        job = self.new_job('ask')
        grant = await self.engine.windows.bind(job['sid'], 'desktop', 'view')
        shared = {'share': grant['share']}
        listing = await self.invoke(job, 'computer_list', shared, 'allow')
        windows = {entry['target']: entry for entry in listing['windows']}
        own = windows[self.initial['target']]
        self.assertEqual(own['title'], self.initial['title'])
        self.assertIn(own['application'], {app['id'] for app in listing['applications']})
        self.assertTrue(any(display['main'] for display in listing['displays']))
        args = dict(shared, target=own['target'])
        frame = await self.invoke(job, 'computer_capture', args, 'allow')
        self.assertEqual(frame['target'], own['target'])
        image = self.captured_image(job, frame)
        point = self.coordinates(frame, 'swatch')
        color = image.pixelColor(point['x'], point['y'])
        self.assertGreater(color.green(), max(color.red(), color.blue()) + 45)
        if self.permissions['accessibility']:
            result = await self.invoke(job, 'computer_inspect', args, 'allow')
            self.assertTrue(any(node['role'] == 'AXButton' and '记录点击' in node.values()
                                for node in result['controls']))
        else:
            with self.assertRaisesRegex(ValueError, '辅助功能'):
                await self.invoke(job, 'computer_inspect', args, 'allow')
        with self.assertRaisesRegex(ValueError, '不允许此操作'):
            await self.invoke(job, 'computer_click', dict(args, frame=frame['frame'], **point))
        self.assertEqual((await self.state())['clicks'], 0)
        await self.engine.windows.stop(job['sid'])
        with self.assertRaisesRegex(ValueError, '共享已停止或更换'):
            await self.invoke(job, 'computer_capture', args)

    @unittest.skipUnless(os.environ.get('MIXHARNESS_TEST_ISOLATED_DESKTOP') == '1',
                         '全屏截图与前台切换仅在专用 macOS 测试桌面设置 MIXHARNESS_TEST_ISOLATED_DESKTOP=1 后运行')
    async def test_display_pixels_and_actions_across_two_test_applications(self):
        if not self.permissions['accessibility']:
            self.skipTest('专用测试桌面需要辅助功能授权才能验证实际输入')
        peer = await self.launch_target(self.root / 'second')
        second = await self.state(target=peer)
        job = self.new_job('auto')
        share = (await self.engine.windows.bind(job['sid'], 'desktop', 'control'))['share']
        listing = await self.invoke(job, 'computer_list', {'share': share})
        windows = {entry['target']: entry for entry in listing['windows']}
        first_id, second_id = self.initial['target'], second['target']
        apps = {entry['id']: entry['bundle'] for entry in listing['applications']}
        first_bundle = apps[windows[first_id]['application']]
        second_bundle = apps[windows[second_id]['application']]
        self.assertNotEqual(first_bundle, second_bundle)
        for bundle in (first_bundle, second_bundle):
            self.assertTrue(bundle.startswith('local.mixharness.tests.window-'))

        async def capture(target):
            return await self.invoke(job, 'computer_capture', {'share': share, 'target': target})

        async def act(target, operation, *, at=None, to=None, **values):
            self.assertIn(target, (first_id, second_id))
            frame = await capture(target)
            for point, x_name, y_name in ((at, 'x', 'y'), (to, 'to_x', 'to_y')):
                if point is not None:
                    values[x_name] = int(point[0] * frame['width'])
                    values[y_name] = int(point[1] * frame['height'])
            return await self.invoke(job, operation, dict(share=share, target=target, frame=frame['frame'], **values),
                                     'allow')

        await act(first_id, 'computer_focus')
        await self.wait_state(lambda state: state['active'] and state['keyWindow'])
        spot = self.initial['swatch']
        left, top, width, height = windows[first_id]['bounds']
        gx, gy = left + width * spot[0], top + height * spot[1]
        display = next(entry for entry in listing['displays']
                       if entry['bounds'][0] <= gx < entry['bounds'][0] + entry['bounds'][2]
                       and entry['bounds'][1] <= gy < entry['bounds'][1] + entry['bounds'][3])
        frame = await capture(display['target'])
        image = self.captured_image(job, frame)
        dx, dy, dw, dh = display['bounds']
        color = image.pixelColor(int((gx - dx) * image.width() / dw), int((gy - dy) * image.height() / dh))
        self.assertGreater(color.green(), max(color.red(), color.blue()) + 45)
        # 显示器的图像不能给另一个窗口的写入操作授权。
        with self.assertRaisesRegex(ValueError, '画面已经失效'):
            await self.invoke(job, 'computer_key', dict(share=share, target=second_id, frame=frame['frame'], key='Tab'))
        await act(first_id, 'computer_click', at=self.initial['button'])
        await self.wait_state(lambda state: state['clicks'] == 1)
        await act(first_id, 'computer_double_click', at=spot)
        await self.wait_state(lambda state: state['doubleClicks'] == 1)
        await act(first_id, 'computer_drag', at=spot, to=(spot[0] + 0.04, spot[1]))
        await self.wait_state(lambda state: state['drags'] == 1)
        before = await self.state('editor')
        await act(first_id, 'computer_scroll', at=before['scrollArea'], delta_y=240)
        await self.wait_state(lambda state: state['scrollY'] > before['scrollY'])
        await act(first_id, 'computer_key', key='Shift+Right')
        await self.wait_state(lambda state: state['selected'] == 1)
        await act(first_id, 'computer_type', text='甲窗口已输入')
        first = await self.wait_state(lambda state: '甲窗口已输入' in state['text'])
        await act(second_id, 'computer_focus')
        await self.wait_state(lambda state: state['active'] and state['keyWindow'], peer)
        await self.state('editor', peer)
        await act(second_id, 'computer_type', text='乙窗口已输入')
        changed = await self.wait_state(lambda state: '乙窗口已输入' in state['text'], peer)
        self.assertNotIn('甲窗口已输入', changed['text'])
        self.assertEqual((await self.state())['text'], first['text'])
        await self.engine.windows.stop(job['sid'])
        self.assertIsNone(self.engine.windows.status(job['sid']))
