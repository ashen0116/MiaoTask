"""Offline activity contracts; no phone or account is controlled."""
import unittest
from unittest.mock import Mock
from backend.app.idlefish.activities.coins.activity import IdleFishCoinsActivity
from backend.core.device.screen import Screen
from backend.core.models import TextNode, Task
from backend.core.errors import TaskUnavailable

class IdlefishContracts(unittest.TestCase):
    def runner(self):
        r = IdleFishCoinsActivity.__new__(IdleFishCoinsActivity)
        r.config = IdleFishCoinsActivity.default_config()
        r.origin = 'com.taobao.idlefish'
        r.control = Mock()
        r.click = Mock()
        r.read = Mock()
        r.device = Mock()
        r.update = Mock()
        r._swipe_once = Mock()
        return r

    def panel(self, progress='0/3', action='去完成'):
        return Screen([TextNode('浏览好物', (100,190,250,210)),
            TextNode(progress, (280,190,330,210)), TextNode(action, (450,190,550,210)),
            TextNode('+100完成3次',(100,80,300,100))],600,1000,'com.taobao.idlefish')

    def search_page(self):
        return Screen([TextNode('搜索',(400,80,500,120)),TextNode('搜索发现',(0,200,100,240)),
                       TextNode('手机壳',(100,400,200,440))],600,1000,'com.taobao.idlefish')

    def test_identity_stays_stable_when_progress_changes(self):
        r=self.runner(); a=self.panel(); b=self.panel('1/3')
        x=r.parse_tasks(a.nodes,a.width,a.height)[0]; y=r.parse_tasks(b.nodes,b.width,b.height)[0]
        self.assertEqual(x.key,y.key); self.assertEqual((x.current,y.current),(0,1))
        self.assertTrue(r.verify(x,a,b))
        self.assertFalse(r.verify(x,a,a))
        r.config['verify_enabled']=False
        self.assertFalse(r.verify(x,a,a))
        self.assertFalse(r.verify(x,a,Screen([],600,1000,r.origin)))

    def test_no_navigation_for_trade_task(self):
        r=self.runner(); task=Task('t','购买商品','','去完成',(0,0,1,1),kind='trade')
        with self.assertRaises(TaskUnavailable):r.execute(task,self.panel())
        r.click.assert_not_called()

    def test_search_returns_contract_tuple(self):
        r=self.runner(); r.config['search_browse_swipes']=1
        s=Screen([TextNode('搜索',(0,0,100,40)),TextNode('搜索发现',(0,200,100,240)),
                  TextNode('手机壳',(100,400,200,440))],600,1000,r.origin)
        r.read.return_value=s
        result=r._search_task(s)
        self.assertIs(result[0],s); self.assertIsInstance(result[1],bool)
        r._swipe_once.assert_called_once()

    def test_search_without_submission_does_not_swipe(self):
        r=self.runner(); s=Screen([],600,1000,r.origin);r.read.return_value=s
        with self.assertRaises(TaskUnavailable):r._search_task(s)
        r._swipe_once.assert_not_called()

    def test_search_inputs_keyword_before_swiping(self):
        r=self.runner(); r.config['search_browse_swipes']=1
        s=self.search_page()
        r.read.return_value=s
        r._search_task(s)
        r.device.input_text.assert_called_once_with(r.config['task_search_keyword'])
        r._swipe_once.assert_called_once()

    def test_search_falls_back_to_suggestion_when_input_unavailable(self):
        r=self.runner(); r.config['search_browse_swipes']=0
        r.device.input_text.side_effect=TimeoutError('synthetic ime failure')
        s=self.search_page()
        r.read.return_value=s
        r._search_task(s)
        # 搜索框一次 + 推荐词一次；输入不可用时不能直接放弃整个任务
        self.assertEqual(r.click.call_count,2)

    def test_browse_keeps_result_when_final_read_overflows(self):
        r=self.runner(); r.control.now.side_effect=[0,0,99999]
        first=self.panel()
        r.read.side_effect=[first,RuntimeError('synthetic StackOverflowError')]
        screen,explicit=r._browse(1)
        self.assertIs(screen,first); self.assertFalse(explicit)

    def test_video_task_waits_for_page_before_finishing(self):
        r=self.runner(); r.config['browse']['video_seconds']=1
        r.config['browse_extra_seconds']=1
        panel=self.panel()
        video=Screen([TextNode('广告',(100,300,200,320))],600,1000,r.origin)
        # 第一屏仍是任务列表（H5 还没跳转），之后始终是视频页
        r.control.now.side_effect=[0,0,5]
        reads={'n':0}
        def read_once(*a,**k):
            reads['n']+=1
            return panel if reads['n']==1 else video
        r.read.side_effect=read_once
        screen,explicit=r._video_browse(1)
        self.assertIs(screen,video); self.assertFalse(explicit)

    def test_restart_client_runs_once_per_round(self):
        r=self.runner(); r._client_restarted=False
        self.assertTrue(r._restart_client())
        self.assertFalse(r._restart_client())
        r.device.go_home.assert_called_once()
        r.device.stop_app.assert_called_once_with(r.origin)
        r.device.start_app.assert_called_once_with(r.origin)

    def test_setup_resets_restart_budget(self):
        r=self.runner(); r._client_restarted=True
        r.setup()
        self.assertFalse(r._client_restarted)

    def test_task_panel_is_not_treated_as_landing(self):
        r=self.runner()
        landing=Screen([TextNode('再走4步完成1圈',(100,800,500,840))],600,1000,r.origin)
        self.assertTrue(r.activity_landing(landing))
        self.assertFalse(r.activity_landing(self.panel()))

    def test_overlay_without_close_text_is_tapped_by_title(self):
        r=self.runner()
        s=Screen([TextNode('前进获得闲鱼币',(100,150,700,200))],600,1000,r.origin)
        self.assertTrue(r._close_overlay(s))
        r.click.assert_called_once()

    def test_unknown_teardown_does_not_claim_or_walk(self):
        r=self.runner();r.read.side_effect=RuntimeError('synthetic disconnected')
        r._claim_top_rewards=Mock();r._walk_steps=Mock()
        r.teardown();r._claim_top_rewards.assert_not_called();r._walk_steps.assert_not_called()

    def test_claim_row_removed_is_verified(self):
        r = self.runner()
        claim = Task('c', '每日签到', '', '领取奖励', (450, 190, 550, 210), kind='claim')
        browse = Task('b', '浏览好物', '', '去完成', (450, 190, 550, 210), kind='browse')
        after = Screen([TextNode('+100完成3次', (100, 80, 300, 100))], 600, 1000, r.origin)
        self.assertTrue(r.verify(claim, self.panel(), after))
        self.assertFalse(r.verify(browse, self.panel(), after))

    def test_claim_ready_does_not_wait_when_not_loading(self):
        r = self.runner()
        task = r.parse_tasks(self.panel().nodes, 600, 1000)[0]
        r._claim_ready(task, self.panel())
        r.read.assert_not_called()
        r.control.wait.assert_not_called()

    def test_claim_ready_allows_one_extra_beat_while_loading(self):
        r = self.runner()
        task = r.parse_tasks(self.panel().nodes, 600, 1000)[0]
        loading = Screen([TextNode('浏览好物', (100, 190, 250, 210)),
                          TextNode('0/3', (280, 190, 330, 210)),
                          TextNode('去完成', (450, 190, 550, 210)),
                          TextNode('加载中', (100, 400, 300, 420)),
                          TextNode('+100完成3次', (100, 80, 300, 100))], 600, 1000, r.origin)
        r.read.return_value = self.panel()
        r._claim_ready(task, loading)
        self.assertEqual(r.read.call_count, 1)
        self.assertEqual(r.control.wait.call_count, 1)

    def test_claim_ready_clicks_reward_immediately(self):
        r = self.runner()
        task = r.parse_tasks(self.panel().nodes, 600, 1000)[0]
        ready = self.panel(action='领取奖励')
        r.read.return_value = ready
        r._claim_ready(task, ready)
        r.click.assert_called_once()

    def test_claim_ready_row_removed_without_receipt_is_not_success(self):
        r = self.runner()
        task = r.parse_tasks(self.panel().nodes, 600, 1000)[0]
        screen = Screen([TextNode('+100完成3次', (100, 80, 300, 100))], 600, 1000, r.origin)
        r._claim_ready(task, screen)
        self.assertNotIn(task.key, getattr(r, '_completed_evidence', set()))
        r.read.assert_not_called()

    def test_exit_dialog_clicks_only_once(self):
        # 同一个弹窗连点两次没变化就停手，不再空耗，也不按返回键。
        r = self.runner()
        first = Screen([TextNode('跳过', (100, 190, 250, 210))], 600, 1000, r.origin)
        r.read.return_value = first  # 点了没变化
        r._at_task_list = Mock(return_value=False)
        r._dismiss_exit_dialogs(first)
        self.assertEqual(r.click.call_count, 1)  # 点一次无效即止
        r.device.go_back.assert_not_called()

    def test_return_to_list_uses_page_back_button_once(self):
        r = self.runner()
        r.device = Mock()
        r.recover = Mock()
        page = Screen([TextNode('返回', (450, 190, 550, 210))], 600, 1000, r.origin)
        r.read.side_effect = [self.panel()]
        r._return_to_list(page)
        r.click.assert_called_once()
        r.device.go_back.assert_not_called()
        r.recover.assert_not_called()
        r.control.wait.assert_called_once_with(1.5)


class ClaimReceiptTests(unittest.TestCase):
    runner = IdlefishContracts.runner
    panel = IdlefishContracts.panel
    def test_click_with_unchanged_claim_button_is_not_success(self):
        r = self.runner(); screen = self.panel(action='领取奖励')
        task = r.parse_tasks(screen.nodes, screen.width, screen.height)[0]
        r._stable_target = Mock(return_value=(screen, task)); r.update = Mock(); r.guard = Mock()
        r._dismiss_popups = Mock(return_value=False); r.read.return_value = screen
        after, explicit, _ = r.execute(task, screen)
        self.assertFalse(explicit)
        self.assertFalse(r.verify(task, screen, after, explicit=explicit))

    def test_visit_skips_fast_when_click_stays_on_list(self):
        r = self.runner()
        r.guard = Mock()
        r._return_to_list = Mock()
        screen = self.panel()
        r.read.return_value = screen
        task = Task(key='v1', title='浏览好物', detail='', action='去完成',
                    bounds=(0, 0, 100, 20), kind='visit')
        r._stable_target = Mock(return_value=(screen, task))
        with self.assertRaises(TaskUnavailable):
            r.execute(task, screen)
        r._return_to_list.assert_not_called()

    def test_refresh_reuses_verified_list_screen(self):
        r = self.runner()
        screen = self.panel()
        r._verified_list_screen = screen
        self.assertIs(r._refresh_after_success(), screen)

    def test_teardown_can_disable_rewards_and_walk(self):
        r = self.runner()
        r.config['teardown_claim_rewards'] = False
        r.config['teardown_walk_steps'] = False
        r.read = Mock()
        r._claim_top_rewards = Mock()
        r._walk_steps = Mock()
        r.teardown()
        r._claim_top_rewards.assert_not_called()
        r._walk_steps.assert_not_called()

    def test_return_resumes_idle_fish_before_cold_start(self):
        r = self.runner()
        r.recover = Mock()
        outside = Screen([], 600, 1000, 'com.taobao.taobao')
        r.read.return_value = self.panel()
        r._return_to_list(outside)
        r.device.resume_app.assert_called_once_with(r.origin)
        r.recover.assert_not_called()

    def test_return_cold_starts_only_when_resume_fails(self):
        r = self.runner()
        r.recover = Mock(return_value=self.panel())
        outside = Screen([], 600, 1000, 'com.taobao.taobao')
        r.device.resume_app.side_effect = RuntimeError('synthetic resume failure')
        r._return_to_list(outside)
        r.recover.assert_called_once_with(cold_start=True)

    def test_close_overlay_closes_all_matches_in_one_screen(self):
        r = self.runner()
        s = Screen([TextNode('×', (100, 100, 120, 120)),
                    TextNode('×', (200, 100, 220, 120)),
                    TextNode('前进获得闲鱼币', (100, 150, 700, 200)),
                    TextNode('前进获得闲鱼币', (100, 300, 700, 350))],
                   600, 1000, r.origin)
        self.assertTrue(r._close_overlay(s))
        self.assertEqual(r.click.call_count, 4)

    def test_exit_dialog_no_back_key_on_noop(self):
        # 弹窗点一次无效后不再按返回键、不再连点，避免空耗（交给外层重启）。
        r = self.runner()
        r.setup()
        dialog = Screen([TextNode('去体验5秒可立即领奖|跳过', (100, 300, 500, 340))],
                        600, 1000, r.origin)
        r.read.return_value = dialog
        r._at_task_list = Mock(return_value=False)
        r._dismiss_exit_dialogs(dialog)
        self.assertEqual(r.click.call_count, 1)
        r.device.go_back.assert_not_called()

    def test_claimed_row_not_parsed_again(self):
        r = self.runner()
        r.setup()
        screen = self.panel(action='领取奖励')
        task = r.parse_tasks(screen.nodes, screen.width, screen.height)[0]
        r._claimed_rows = {(task.key, task.current, '领取奖励')}
        self.assertEqual(r.parse_tasks(screen.nodes, screen.width, screen.height), [])

    def test_parse_tasks_skips_uninstalled_external_app(self):
        r = self.runner()
        r.device.installed_packages = Mock(return_value=[])
        s = Screen([TextNode('去百亿补贴领红包', (100, 190, 400, 210)),
                    TextNode('去完成', (450, 190, 550, 210)),
                    TextNode('浏览好物', (100, 290, 400, 310)),
                    TextNode('去完成', (450, 290, 550, 310))],
                   600, 1000, r.origin)
        tasks = r.parse_tasks(s.nodes, s.width, s.height)
        self.assertEqual([t.title for t in tasks], ['浏览好物'])

    def test_overlay_without_close_does_not_go_back(self):
        # 浮层关不掉时按返回键会把 H5 落地页整层退掉，框架随后逐层 go_back
        # 退到桌面判失败。修复后应留在落地页等待，绝不调用系统返回键，
        # 且重算时优先走强制 OCR 兜底入口再点一次。
        r = self.runner()
        r.guard = Mock()
        r.config['overlay_wait_seconds'] = 0
        r._close_overlay = Mock(return_value=False)
        landing = Screen([TextNode('再走4步完成1圈', (100, 800, 500, 840))],
                         600, 1000, r.origin)
        entry = TextNode('赚更多', (500, 800, 560, 830), 'ocr')
        r.task_panel_visible = Mock(return_value=False)
        r.panel_loading = Mock(return_value=False)
        # 第一次进落地页找到入口点下去，但面板一直不出（浮层挡住），
        # 迫使代码走到重试分支；重试时 _visual_entry(force=True) 再找入口。
        r.task_panel_entry = Mock(return_value=entry)
        r._visual_entry = Mock(return_value=entry)
        r._accept_list = Mock(return_value=False)
        ticks = {'n': 0}
        def now():
            ticks['n'] += 1
            return 99999 if ticks['n'] > 8 else 0  # 几次循环后越过 deadline
        r.control.now.side_effect = now
        r.read.return_value = landing
        r.enter_task_panel(landing)
        r.device.go_back.assert_not_called()
        r._visual_entry.assert_called()
        self.assertTrue(any(c.kwargs.get('force') for c in r._visual_entry.call_args_list))

    def test_last_ditch_recover_opens_panel_from_landing(self):
        r = self.runner()
        landing = Screen([TextNode('再走4步完成1圈', (100, 800, 500, 840))],
                         600, 1000, r.origin)
        ready = self.panel()
        r.read.return_value = landing
        r.activity_landing = Mock(return_value=True)
        r.enter_task_panel = Mock(return_value=ready)
        # 第一次 _accept_list(落地页)=False 才会去 enter_task_panel；
        # 第二次 _accept_list(面板)=True 才返回成功。
        r._accept_list = Mock(side_effect=[False, True])
        result = r._last_ditch_recover(RuntimeError('navigation failed'))
        self.assertIs(result, ready)
        r.enter_task_panel.assert_called_once()

    def test_last_ditch_recover_reraises_when_off_app(self):
        r = self.runner()
        desktop = Screen([], 600, 1000, 'com.miui.home')
        r.read.return_value = desktop
        with self.assertRaises(RuntimeError):
            r._last_ditch_recover(RuntimeError('navigation failed'))

    def test_guard_ignores_single_black_video_frame(self):
        # 视频黑场单帧被核心判成 obscured，重读后恢复就应放行，不整轮停。
        from backend.core.errors import NeedsAttention
        r = self.runner()
        r.assert_instance = Mock()
        r.config['obscured_confirm_frames'] = 3
        r.config['obscured_confirm_interval'] = 0
        black = Screen([], 600, 1000, r.origin, obscured=True)
        normal = self.panel()
        r.read.return_value = normal  # 重读第一帧就恢复
        r.guard(black)  # 不应抛异常
        r.read.assert_called()

    def test_guard_stops_on_persistent_black_screen(self):
        # 连续多帧都黑才是真的受保护页面，照常抛 NeedsAttention。
        from backend.core.errors import NeedsAttention
        r = self.runner()
        r.assert_instance = Mock()
        r.report_failure = Mock(return_value=None)
        r.config['obscured_confirm_frames'] = 3
        r.config['obscured_confirm_interval'] = 0
        black = Screen([], 600, 1000, r.origin, obscured=True)
        r.read.return_value = black
        with self.assertRaises(NeedsAttention):
            r.guard(black)
