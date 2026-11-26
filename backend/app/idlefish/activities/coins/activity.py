"""闲鱼币大富翁（严格按人工操作流程实现）。

只保留与人工操作一致的步骤：
1. 打开闲鱼 → 我的 → 闲鱼币 → 大富翁页面（交给 EntryPolicy 的导航钩子）
2. 大富翁页面上「前进获得闲鱼币」右侧那颗圆形按钮同时显示「赚更多」和「领」，
   就是任务列表入口，点击进入（EntryPolicy.task_panel_entry）
3. 任务列表内：
   - 优先点「签到」「领取奖励」
   - 再识别浏览类任务（看视频等），按要求的秒数持续浏览，返回列表领奖
   - 当前视口没有浏览任务就下滑继续找（由框架 SessionLoop 负责滚动）
4. 任务列表顶部的「累计任务奖励」达标后顺手领取
   （+100完成3次 / +300完成6次 / +500完成10次）
5. 回到大富翁页面，点「前进获得闲鱼币」直到按钮不再显示可用步数
"""
import hashlib
import logging
import random
import re

from backend.core.config.loader import load_config
from backend.core.errors import TaskUnavailable
from backend.core.models import Task, TextNode, normalize
from backend.core.execution.activity import Activity

from backend.app.idlefish.manifest import PROFILE
from .entry import EntryPolicy

logger = logging.getLogger('backend.execution')

# 任务行右侧的动作按钮
ACTION_RE = r'^(签到|领取奖励|立即领取|去完成|去浏览)$'
# 累计任务奖励卡片：未完成时是 +100完成3次，达到进度后变成 +100领取奖励
CUMULATIVE_RE = r'^\+\d+(?:完成\d+次|领取奖励)$'
# 已达成的累计奖励卡片（只有这种才可点，未达成的点了也没用）
CUMULATIVE_READY_RE = r'^\+\d+领取奖励$'
# 「前进获得闲鱼币」按钮
STEP_RE = r'再走\d+步完成1圈'
# 可用步数显示在按钮右上方，形如「再走4步完成1圈 5步」末尾的「5步」
STEP_LEFT_RE = r'(\d+)步$'
# 页面自己给出的完成提示：出现即认定任务已达标
DONE_PROMPT_RE = r'^(?:任务已完成|浏览已完成|浏览完成|完成浏览|奖励已到账|恭喜完成|领取成功|已领取)[!！。\s]*$'
# 浏览页倒计时（「还需12秒 / 剩余8秒」），用来动态调整结束时间
COUNTDOWN_RE = r'(?:还需|剩余|再逛|再看|浏览还差)[^\d]{0,3}(\d{1,2})\s*秒'
# 进入浏览页时的总时长提示（「浏览20秒获得100闲鱼币」）
BROWSE_SECONDS_RE = r'浏览\s*(\d+)\s*[秒sS]'
# 视频页自带的倒计时（「奖励将于14秒后发放」），按它等而不必死等配置秒数
VIDEO_REWARD_COUNTDOWN_RE = r'奖励将于\s*(\d+)\s*秒后发放'
# 二级确认弹窗标题；各种写法都要认：确认要离开吗 / 坚持退出 / 退出 /
# 残忍离开 / 放弃奖励离开… 只认前两种会让「坚持退出」「退出」这类弹窗漏掉，
# 脚本找不到弹窗就去按返回键，退不出去最后只能杀 APP 重启，白耗二十多秒。
VIDEO_LEAVE_DIALOG_RE = (r'确认要离开吗|放弃奖励离开|坚持退出|残忍离开|'
                         r'确认退出|确定退出|狠心退出|坚持离开|确认离开|'
                         r'^退出$|退出任务|离开任务')


class IdleFishCoinsActivity(EntryPolicy, Activity):
    application = PROFILE

    def guard(self, screen):
        """视频页黑场会被核心 guard 误判成「受保护页面」而整轮停掉。

        核心 screen 判定是「单帧最亮像素 < 8 就是 obscured」，视频播放/转场的
        一帧全黑就中招（新设备上更常见），随后 check_attention 直接 NeedsAttention
        收工。这里在闲鱼活动内做二次确认：闲鱼前台的单帧黑屏先重读几帧，只要
        有一帧恢复就放行；连续多帧都黑才认定为真的受保护页面，交回核心处理。
        验证码/登录/支付等其它拦截一律照常走核心判定，不放宽。
        """
        self.assert_instance(screen)
        from backend.core.execution.guard import check_attention, attention_reason, OBSCURED_REASON
        if (getattr(screen, 'obscured', False)
                and (screen.package or '').startswith('com.taobao.idlefish')):
            found = attention_reason(self, screen)
            # 只有当拦截原因「就是黑屏」时才二次确认；验证码/登录等其它证据
            # 在 attention_reason 里排在黑屏之前，found[0] 不是黑屏文案就照常拦。
            if found is not None and found[0] == OBSCURED_REASON:
                tries = int(self.config.get('obscured_confirm_frames', 3))
                for _ in range(max(1, tries)):
                    self.control.wait(float(self.config.get('obscured_confirm_interval', 1.0)))
                    try:
                        fresh = self.read(force_ocr=bool(self.config.get('ocr', False)))
                    except Exception:
                        continue
                    self.assert_instance(fresh)
                    if not getattr(fresh, 'obscured', False) and attention_reason(self, fresh) is None:
                        logger.info('黑屏为视频转场帧，已确认恢复，继续执行')
                        return
                logger.warning('连续多帧画面不可读取，按受保护页面处理')
        check_attention(self, screen)

    def read(self, **kwargs):
        screen = super().read(**kwargs)
        self.guard(screen)
        return screen

    def _safe_read(self, screen=None, force_ocr=False):
        """读屏失败时沿用上一屏，不中断当前任务。

        闲鱼活动页是 H5 容器，浏览类任务的商品瀑布流滚得越久节点越多；
        uiautomator 导出控件树时可能抛出 StackOverflowError（Java 栈溢出，
        实测 20 秒连续滑动后必现）。这类崩溃只影响「看一眼」这一步，
        不应该让整个任务作废：返回上一屏，由调用方按「没有完成提示」
        继续走返回列表与核验流程。
        """
        try:
            return self.read(force_ocr=force_ocr)
        except Exception as exc:
            logger.warning('读取屏幕失败，沿用上一屏继续：%s', exc)
            return screen

    # ------------------------------------------------------------------
    # 契约：配置
    # ------------------------------------------------------------------
    @staticmethod
    def default_config():
        return load_config(app='idlefish', activity='coins')

    # ------------------------------------------------------------------
    # 契约：任务列表识别
    # ------------------------------------------------------------------
    def is_task_list(self, nodes):
        """任务列表特征：右侧至少两个动作按钮，或出现累计任务奖励卡片。

        不能只看单个「签到」字样——首页和「我的」页也有「现金签到」等文案，
        只靠词表会把普通页面误判成任务列表，进而在首页上执行假任务。
        """
        texts = [normalize(n.text) for n in nodes]
        actions = sum(1 for t in texts if re.fullmatch(ACTION_RE, t))
        if actions >= 2:
            return True
        return any(re.fullmatch(CUMULATIVE_RE, t) for t in texts)

    # 任务标题里能确定的外部 App。只有确认设备没装时才在列表阶段跳过，
    # 免得点进去、等二十多秒、再导航回来才知道没装。映射不确定时宁可不跳：
    # 读不到已安装列表也一律不过滤，避免误杀本来能做的任务。
    EXTERNAL_APP_MAP = {
        '百亿补贴': ('com.taobao.taobao',),
        '闪购': ('com.taobao.taobao',),
        '淘宝': ('com.taobao.taobao',),
        '天猫': ('com.tmall.wireless', 'com.taobao.taobao'),
        '一淘': ('com.taobao.etao', 'com.taobao.taobao'),
        '点淘': ('com.taobao.litetao', 'com.taobao.taobao'),
        '支付宝': ('com.eg.android.AlipayGphone',),
        '蚂蚁': ('com.eg.android.AlipayGphone',),
        '蚂蚁森林': ('com.eg.android.AlipayGphone',),
        '芭芭农场': ('com.eg.android.AlipayGphone',),
        '口碑': ('com.eg.android.AlipayGphone',),
        '闪购': ('com.taobao.taobao',),
        '一淘': ('com.taobao.etao', 'com.taobao.taobao'),
        '天猫': ('com.tmall.wireless', 'com.taobao.taobao'),
        '聚划算': ('com.taobao.taobao',),
        '淘金币': ('com.taobao.taobao',),
        '点淘': ('com.taobao.litetao', 'com.taobao.taobao'),
        '大众点评': ('com.dianping.v1',),
        '点评': ('com.dianping.v1',),
        '美团': ('com.sankuai.meituan',),
        '外卖': ('com.sankuai.meituan',),
        '百度地图': ('com.baidu.BaiduMap', 'com.baidu.map'),
        '高德': ('com.autonavi.minimap',),
        '京东': ('com.jingdong.app.mall',),
        '抖音': ('com.ss.android.ugc.aweme',),
        '快手': ('com.kuaishou.nebula', 'com.ghb.constants'),
        '饿了么': ('com.eg.android.AlipayGphone', 'me.ele'),
        '淘票票': ('com.taobao.taobao',),
        '飞猪': ('com.taobao.taobao', 'com.trip.taobao'),
    }

    @classmethod
    def _has_external_app_word(cls, title):
        """标题里是否带明确的外部 App 词（淘宝/闪购/支付宝…）。

        用来区分站内浏览任务和跳到外部 App 的访问任务：带这些词的「逛逛」
        是跳转，点进去即算完成，不能当浏览白滑。
        """
        return any(word in title for word in cls.EXTERNAL_APP_MAP)

    def _target_app_missing(self, title):
        packages = next((pkgs for word, pkgs in self.EXTERNAL_APP_MAP.items()
                         if word in title), None)
        if not packages:
            return False
        if not hasattr(self, '_idle_installed_packages'):
            try:
                found = self.device.installed_packages()
                # 空列表是「一个 App 都没装」，不是读取失败；别用真假值判断
                self._idle_installed_packages = (set(found)
                                                 if isinstance(found, (set, list, tuple))
                                                 else None)
            except Exception as exc:
                logger.debug('读取已安装应用列表失败，不提前过滤任务：%s', exc)
                self._idle_installed_packages = None
        installed = self._idle_installed_packages
        if installed is None:
            return False
        return not any(p in installed for p in packages)

    # ------------------------------------------------------------------
    # 契约：解析任务（签到 / 领取奖励 / 去完成）
    # ------------------------------------------------------------------
    def parse_tasks(self, nodes, width, height):
        tasks = []
        buttons = [n for n in nodes
                   if re.fullmatch(ACTION_RE, normalize(n.text))
                   and n.center[0] > width * 0.55]
        titles = [n for n in nodes
                  if width * 0.12 <= n.bounds[0] <= width * 0.35
                  and n.bounds[2] <= width * 0.75
                  and 0 < n.bounds[3] - n.bounds[1] < height * 0.05
                  and re.search(r'[\u4e00-\u9fff]', n.text)
                  and not re.fullmatch(ACTION_RE, normalize(n.text))
                  and not re.fullmatch(CUMULATIVE_RE, normalize(n.text))
                  # 「收益+10%」这类红色奖励标签紧贴按钮，OCR 常把它读成一行
                  # 乱码标题再跟按钮配对，生成根本不存在的任务
                  and not re.search(r'收益|[+＋]\s*\d+\s*%|\d+%', normalize(n.text))]
        for btn in buttons:
            action = normalize(btn.text)
            # 标题与按钮同一行；「签到」是独立按钮，没有同行标题
            row = [n for n in titles
                   if abs(n.center[1] - btn.center[1]) < height * 0.02
                   and btn.bounds[0] - n.bounds[2] > width * 0.06]
            if row:
                title_node = min(row, key=lambda n: (n.bounds[0],
                                                    abs(n.center[1] - btn.center[1])))
                title = normalize(title_node.text)
            elif action == '签到':
                title = '每日签到'
            else:
                continue
            if any(k in title for k in self.config['game_keywords']):
                continue
            if any(k in title for k in self.config['skip_titles']):
                continue
            if self._target_app_missing(title):
                # 同一任务每扫一屏都会重新解析，日志去重，别刷屏
                if title not in getattr(self, '_skip_logged', set()):
                    logger.info('目标应用未安装，列表中直接跳过：%s', title)
                    self._skip_logged = getattr(self, '_skip_logged', set()) | {title}
                continue
            current, total = self._progress(nodes, btn, height)
            seconds = (self.config['browse']['video_seconds']
                       if self._is_video(title)
                       else self.config['browse']['regular_seconds'])
            # Stable identity: progress belongs to framework verification, not the key.
            key = hashlib.sha256(title.encode()).hexdigest()[:16]
            # 刚点过「领取奖励」的同一档行先别再解析。页面刷新慢时框架下一轮
            # 会再点一次同一个按钮，白花 5～8 秒；进度变了（新的一档）不受影响。
            if (key, current, action) in getattr(self, '_claimed_rows', set()):
                continue
            tasks.append(Task(key, title, title, action, btn.bounds,
                              current=current, total=total, seconds=seconds,
                              kind=self._classify(title, action)))
        return tasks

    @staticmethod
    def _is_video(title):
        return '视频' in title or '看15秒' in title or '看30秒' in title

    # 小游戏容器特征：XRiverActivity 常驻「游戏任务入口」，退出时弹
    # 「再玩1关游戏即可完成任务 / 残忍离开 / 继续玩」。
    GAME_SIGNS_RE = r'残忍离开|继续玩|再玩\d+关|游戏任务入口|即可完成任务'

    def _is_game_container(self, screen):
        return self._find(screen, self.GAME_SIGNS_RE) is not None

    def _exit_game(self):
        """退出小游戏容器，尽量回到任务列表。"""
        for _ in range(4):
            screen = self.read()
            if self._find(screen, r'残忍离开') is not None:
                self._click_text(screen, r'残忍离开')
                self.control.wait(2)
                continue
            if self._find(screen, r'游戏任务入口') is not None:
                # 点右上角关闭图标，触发「残忍离开」确认弹窗
                self.device.click(int(screen.width * .92), int(screen.height * .075))
                self.control.wait(2)
                continue
            if self.is_task_list(screen.nodes):
                return True
            self.device.go_back()
            self.control.wait(2)
        return self.is_task_list(self.read().nodes)

    def _classify(self, title, action):
        if action in ('签到', '领取奖励', '立即领取'):
            return 'claim'
        # 真实交易类（如「去浏览全新好物下单」）：涉及真实下单付款，默认跳过
        if '下单' in title or '购买' in title or '拍下' in title:
            return 'trade'
        # 搜索类任务（如「搜一搜推荐商品」）：进入搜索页后还需输入关键词并浏览
        if '搜一搜' in title or '搜索' in title or '搜' in title:
            return 'search'
        # 「看视频 / 浏览xx」都是闲鱼内的浏览任务（已实机验证：「去浏览福利好物」
        # 进入 CsjMall 页并提示「浏览15秒获得80闲鱼币」）。
        # 关键：标题里带明确外部 App 词（淘宝/闪购/芭芭农场/支付宝…）的是跳转
        # 任务，即使写着「逛逛」也不能滑动——「逛逛淘宝闪购天天免单」跳到淘宝
        # 闪购，点进去即算完成，以前按「逛逛」判浏览白滑 22 秒还核验失败。
        # 只有站内「逛逛商城」这类（无外部 App 词）才滑动浏览。
        if not self._has_external_app_word(title) and (
                self._is_video(title) or '浏览' in title or '逛逛' in title):
            return 'browse'
        # 「点击指定频道好物」类任务已停用（用户要求不做）：归入 trade（跳过），
        # 由 enabled_task_kinds 过滤，框架直接不执行，也不会再点商品卡片。
        if self._is_click_task(title):
            return 'trade'
        # 其余「去完成」按跨应用处理，由 enabled_task_kinds 过滤掉。
        return 'visit'

    @staticmethod
    def _is_click_task(title):
        """点击商品类任务：仅「点击指定频道好物」这类明确要求逐个点商品的标题。

        不能按「逛」字泛匹配——「逛逛商城领超值优惠券」等只是逛逛商城，
        在那里逐个点「抵后价」商品无效，会白耗时间。
        """
        return '点击' in title and any(k in title for k in ('好物', '宝贝', '商品', '频道'))

    @staticmethod
    def _progress(nodes, btn, height):
        """读取同一行的「(0/4)」进度。"""
        row = sorted((n for n in nodes if abs(n.center[1] - btn.center[1]) < height * 0.02),
                     key=lambda n: n.bounds[0])
        match = re.search(r'(\d+)/(\d+)', ''.join(normalize(n.text) for n in row))
        return (int(match.group(1)), int(match.group(2))) if match else (0, 1)

    # ------------------------------------------------------------------
    # 契约：执行单个任务
    # ------------------------------------------------------------------
    def _stable_target(self, title, tries=2):
        """连续两次读取到同一坐标，才认为任务列表已经稳定。

        列表点击后会自行重排：完成的任务跳首行、领奖后整行消失、后面各行整体上移，
        在重排动画中间态点击极易落到相邻行，所以必须等坐标稳定再动手。
        按标题匹配；两次读数不同使用最新位置，进度由核验层处理。
        """
        screen, last = None, None
        for _ in range(tries):
            screen = self.read()
            current = next((t for t in self.parse_tasks(screen.nodes, screen.width, screen.height)
                            if t.title == title), None)
            if current is None:
                return screen, None
            if last is not None and last.bounds == current.bounds:
                return screen, current
            last = current
            self.control.wait(0.2)
        return screen, last

    def execute(self, task, before):
        if task.kind == 'trade':
            raise TaskUnavailable('该任务不自动执行')
        self._completed_evidence = set()
        # 每次点击前都按当前页面重新识别，绝不使用外层缓存的坐标
        screen, fresh = self._stable_target(task.title)
        if fresh is None:
            return screen, False, False
        if fresh.kind == 'trade':
            raise TaskUnavailable('该任务不自动执行')
        task = fresh
        self.update('opening', current_task=task.title, message='开始执行')
        logger.info('点击任务：%s | 按钮=%s | 区域=%s', task.title, fresh.action, fresh.bounds)
        self.click(TextNode(fresh.action, fresh.bounds))
        if task.kind == 'claim':
            # 「领取奖励」按钮点掉后整行约 3 秒才消失，那是页面自己的刷新，
            # 不需要脚本反复读屏核验，也不靠「领取成功/奖励已到账」等文案判定。
            # 点击本身不作为成功证据，交给回执或任务行变化核验。
            self.control.wait(float(self.config.get('claim_settle_seconds', 0.6)))
            screen = self.read()
            self.guard(screen)
            explicit = bool(screen.find(DONE_PROMPT_RE))
            if self._dismiss_popups(screen):
                screen = self.read()
            return screen, explicit, False
        self.control.wait(2)
        screen = self.read()
        if task.kind == 'trade':
            # 下单购买等真实交易类、以及已停用的「点击指定频道好物」类任务：
            # 不自动执行，直接跳过
            logger.info('跳过不执行的任务：%s', task.title)
            return self.recover(), False, False
        if task.kind == 'search':
            # 搜索类：进入搜索页后搜词并浏览
            screen, explicit = self._search_task(screen)
            return self._claim_ready(task, self._return_to_list(), explicit=explicit), explicit, False
        if task.kind == 'visit':
            # 跳转类：点「去完成」跳到其它 App，停留几秒后回闲鱼。
            # 未安装目标 App 时跳转根本不会发生，以前仍等满 visit_seconds 再走
            # 整套返回导航，一个做不了的任务白耗 25～30 秒。这里先短等确认是否
            # 真的离开了闲鱼：点了还停在任务列表，就直接判不可达跳过。
            quick = max(0.5, float(self.config.get('visit_quick_check_seconds', 2.0)))
            self.control.wait(quick)
            screen = self.read()
            self.guard(screen)
            if self._still_on_list(screen):
                # 再给一拍，排除服务端慢半拍才跳转的情况
                self.control.wait(1)
                screen = self.read()
                self.guard(screen)
                if self._still_on_list(screen):
                    logger.info('点击后仍停留在任务列表，判定跳转未生效：%s', task.title)
                    raise TaskUnavailable('跳转任务未完成（目标App可能未安装），跳过：%s' % task.title)
            remain = float(self.config.get('visit_seconds', 5)) - quick
            if remain > 0:
                self.control.wait(remain)
            screen = self._return_to_list(cold_start=False)
            if not self._visit_done(task, screen):
                raise TaskUnavailable('跳转任务未完成（目标App可能未安装），跳过：%s' % task.title)
            return self._claim_ready(task, screen), False, False
        # 浏览类：跳进小游戏就退出跳过；否则立刻开始持续滑动。
        # 商品页靠滑动计时，任何停顿（弹窗处理、二次检测）都会中断计时。
        if self._is_game_container(screen):
            self._exit_game()
            raise TaskUnavailable('该任务实际是小游戏，已退出并跳过')
        if self._is_video(task.title):
            screen, explicit = self._video_browse(task.seconds)
        else:
            screen, explicit = self._browse(task.seconds)
        # 浏览完主动返回任务列表，顺手把变成「领取奖励」的行领掉。
        # 把浏览结束时那一屏传进去：读屏刚崩过时不用再读一次整屏。
        return self._claim_ready(task, self._return_to_list(screen), explicit=explicit), explicit, False

    def _claim_ready(self, task, screen, tries=2, explicit=False):
        """浏览/跳转完成后顺手领奖：默认只看一拍，页面仍在加载才再看一拍。

        服务端结算慢时不再原地死等三拍，把领奖交给下一轮扫描（框架给领取类
        排的优先级最高）；整行消失直接当完成，不再空耗读屏。
        """
        total = max(1, tries)
        if screen is None:
            return screen
        for index in range(total):
            if not self.is_task_list(screen.nodes):
                return screen
            row = next((t for t in self.parse_tasks(screen.nodes, screen.width, screen.height)
                        if t.title == task.title), None)
            if row is not None and row.action in ('领取奖励', '立即领取'):
                self._completed_evidence = getattr(self, '_completed_evidence', set()) | {task.key}
                self._claimed_rows = getattr(self, '_claimed_rows', set()) | {
                    (task.key, task.current, row.action)}
                logger.info('任务已完成，领取奖励：%s', task.title)
                self.click(TextNode(row.action, row.bounds))
                self.control.wait(float(self.config.get('claim_settle_seconds', 0.6)))
                self._dismiss_popups(self._safe_read(screen))
                return self._safe_read(screen)
            if row is None:
                # 行不在当前视口不能证明浏览完成；保留明确完成回执。
                if explicit:
                    self._completed_evidence = getattr(self, '_completed_evidence', set()) | {task.key}
                logger.info('任务行已消失，后续交给下一轮扫描：%s', task.title)
                return screen
            # 只有页面仍在「加载中」才多给一拍，否则直接返回交给下一轮扫描。
            # 浏览页给过完成提示时，领奖延后不代表本轮失败。
            if index + 1 >= total or not self.panel_loading(screen):
                if explicit:
                    self._completed_evidence = getattr(self, '_completed_evidence', set()) | {task.key}
                return screen
            self.control.wait(1)
            screen = self._safe_read(screen)
        return screen

    # ------------------------------------------------------------------
    # 契约：核验
    # ------------------------------------------------------------------
    def _verify_impl(self, task, before, after, explicit=False):
        """Count only same-task progress, reward eligibility or explicit receipts."""
        if after.package != self.origin:
            return False
        if explicit or task.key in getattr(self, '_completed_evidence', set()):
            return True
        if not self.is_task_list(after.nodes):
            return False
        tasks = self.parse_tasks(after.nodes, after.width, after.height)
        current = next((t for t in tasks if t.title == task.title), None)
        if current is not None:
            if current.current > task.current:
                return True
            # 按钮从「去完成」变「领取奖励」：任务已达标，只是还没领
            if task.kind != 'claim' and current.kind == 'claim':
                return True
            if task.kind == 'claim' and current.kind != 'claim':
                return True
            return bool(explicit) and current.kind == 'claim'
        # 任务行消失：领取类视为已领；浏览类要求页面给过完成提示
        return task.kind == 'claim'

    def verify(self, task, before, after, explicit=False):
        ok = self._verify_impl(task, before, after, explicit)
        if ok and after is not None and getattr(after, 'nodes', None):
            try:
                if self.is_task_list(after.nodes):
                    self._verified_list_screen = after
            except Exception:
                pass
        return ok

    def _refresh_after_success(self, task=None):
        """成功后复用刚核验过的列表屏，省掉框架默认的一次截图+OCR。

        活动执行末尾已经回到任务列表并读过屏；核验成功时那一屏就是最新列表，
        再让框架读一遍纯属重复。缓存只留一拍，非列表屏仍走框架原有恢复逻辑。
        """
        cached = getattr(self, '_verified_list_screen', None)
        self._verified_list_screen = None
        if cached is not None and self.is_task_list(cached.nodes):
            self.control.check()
            return cached
        return super()._refresh_after_success(task)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def setup(self):
        # 导航（闲鱼 → 我的 → 闲鱼币 → 赚更多）完全交给框架 ensure_list，
        # 进入后不做任何地图滑动或掷骰子，避免误触。
        self._client_restarted = False
        self._skip_logged = set()
        self._claimed_rows = set()

    def teardown(self):
        claim = bool(self.config.get('teardown_claim_rewards', True))
        walk = bool(self.config.get('teardown_walk_steps', True))
        if not claim and not walk:
            logger.info('按配置关闭累计奖励与走步数，直接收工')
            return
        # 只有确实没有待执行任务时才收尾；否则把累计奖励和走步数留到下一轮，
        # 避免「任务还没做完就先走步数」（例如达到 max-tasks 提前结束）。
        try:
            screen = self.read()
            self.guard(screen)
            if self.is_task_list(screen.nodes) and self._has_pending(screen):
                logger.info('仍有待执行任务，本轮跳过累计奖励与走步数')
                return
        except Exception as exc:
            logger.info('收尾前检查未通过，停止收尾：%s', exc)
            return
        if claim:
            try:
                self._claim_top_rewards()
            except Exception as exc:
                logger.warning('领取累计任务奖励失败（已忽略）：%s', exc)
        if walk:
            try:
                self._back_to_coin_page()
                self._walk_steps()
            except Exception as exc:
                logger.warning('走步数阶段异常（已忽略）：%s', exc)

    def _has_pending(self, screen):
        """任务列表里是否还有本活动要做的「去完成」任务。"""
        enabled = self.config.get('enabled_task_kinds', [])
        return any(t.kind in enabled and t.action in ('去完成', '去浏览')
                   for t in self.parse_tasks(screen.nodes, screen.width, screen.height))

    # ------------------------------------------------------------------
    # 累计任务奖励：每轮回到任务列表都查一次，不必等收尾
    # ------------------------------------------------------------------
    def _claim_top_rewards(self, screen=None):
        """领取任务列表顶部的累计任务奖励卡片。

        卡片未达成时是「+100完成3次」，达标后文案变成「+100领取奖励」；
        只在已达成时点击，未达成的点了也没用。
        """
        if screen is None:
            screen = self.read()
        if not self.is_task_list(screen.nodes):
            try:
                screen = self.recover()
            except Exception as exc:
                logger.warning('未回到任务列表，跳过累计奖励：%s', exc)
                return screen
            if not self.is_task_list(screen.nodes):
                return screen
        cards = [n for n in screen.nodes
                 if re.fullmatch(CUMULATIVE_READY_RE, normalize(n.text))]
        if not cards:
            return screen
        self.update(message='领取累计任务奖励')
        for card in cards:
            logger.info('领取累计任务奖励：%s', card.text)
            self.click(card)
            self.control.wait(1)
            self._dismiss_popups(self.read())
        return self.read()

    # ------------------------------------------------------------------
    # 收尾 2：回到大富翁主页面
    # ------------------------------------------------------------------
    def _back_to_coin_page(self):
        """回到大富翁主页面：先关任务面板；若已跑出活动页则重新导航进去。"""
        for _ in range(4):
            screen = self.read()
            if self.activity_landing(screen) and not self.is_task_list(screen.nodes):
                return screen
            if not self.is_task_list(screen.nodes):
                break
            self.device.go_back()
            self.control.wait(1)
        try:
            self.ensure_list(auto_open=True)
        except Exception as exc:
            logger.warning('重新进入活动失败，跳过走步数：%s', exc)
            return self.read()
        for _ in range(4):
            screen = self.read()
            if not self.is_task_list(screen.nodes):
                return screen
            self.device.go_back()
            self.control.wait(1)
        return self.read()

    # ------------------------------------------------------------------
    # 收尾 3：点「前进获得闲鱼币」直到没有可用步数
    # ------------------------------------------------------------------
    def _walk_steps(self):
        last_left = None
        same = 0
        for _ in range(40):
            screen = self.read()
            if not self.activity_landing(screen):
                break
            step = self._step_button(screen) or self._find(screen, STEP_RE)
            if step is None:
                break
            match = re.search(STEP_LEFT_RE, normalize(step.text))
            left = int(match.group(1)) if match else None
            if left is not None and left <= 0:
                logger.info('「前进」不再显示可用步数，停止走步')
                break
            # 步数连续两次没变化，说明已经走不动（或被限次），提前收尾
            if left is not None and left == last_left:
                same += 1
                if same >= 2:
                    logger.info('步数连续未变化（%s步），停止走步', left)
                    break
            else:
                same = 0
            last_left = left
            # 读不到「N步」也继续点：能不能继续走，以按钮是否还在为准
            self.update(message='走步数，剩余 %s 步' % (left if left is not None else '?'))
            logger.info('剩余 %s 步，点击前进', left if left is not None else '?')
            self.click(step)
            self.control.wait(1.5)
            self._dismiss_popups(self.read())

    # ------------------------------------------------------------------
    # 浏览动作
    # ------------------------------------------------------------------
    def _at_task_list(self, screen):
        """是否已在闲鱼任务列表（跨 App 时 package 会变，必须一并判断）。"""
        return ((screen.package or '').startswith('com.taobao.idlefish')
                and self.is_task_list(screen.nodes))

    # 仍在视频/广告全屏页的特征（任务列表里虽也有「广告」标记，但不会有
    # 这些专属词，故用它们判定「尚未退出视频页」而不是「停在任务列表」）
    _AD_VIDEO_RE = r'重播|再来一条|立即下载|关闭广告|跳过广告|短视频|视频播放|已观看'

    def _is_ad_or_video_page(self, screen):
        return self._find(screen, self._AD_VIDEO_RE) is not None

    def _dismiss_exit_dialogs(self, screen, tries=None):
        """广告/视频页的退出确认，只点一次，点不就走就让外层重启重进。

        实测「去体验15秒可立即领奖|跳过」这类弹窗点了根本不回列表（OCR 把
        两个按钮读成一个节点，点中心落在中间没反应），后面还会再弹一个；
        旧实现连点 4 次、两次改按返回键，空耗十几秒，最后仍是靠杀 APP 重启
        才回到任务列表。既然终点都是重启，这里点一次无效就立刻停手，把屏
        交回外层走恢复，省掉无效连点。真能一次关掉的弹窗仍然受用，不亏。
        """
        words = [str(w) for w in (list(self.config.get('video_skip_words') or [])
                                  + list(self.config.get('video_exit_words') or [])) if w]
        if not words:
            return screen
        if self._at_task_list(screen):
            return screen
        pattern = '|'.join(re.escape(w) for w in words)
        settle = float(self.config.get('exit_dialog_interval', 1.0))
        node = self._find(screen, pattern)
        if node is None:
            return screen
        logger.info('退出确认弹窗，点击「%s」', node.text)
        self.click(node)
        self.control.wait(settle)
        screen = self.read(force_ocr=bool(self.config.get('ocr', False)))
        if self._at_task_list(screen):
            return screen
        # 弹窗还在（或点了没反应）：不再连点、不再按返回键，交回外层重启重进。
        logger.info('退出弹窗点击后未回到列表，交给恢复流程重进活动')
        return screen

    def _video_leave_button(self, screen):
        """二级弹窗里真正「退出/离开」的按钮；绝不点「抓住奖励机会」。

        弹窗标题（「观看满30秒即可获得奖励 确认要离开吗？」）里也带「离开」，
        直接 find('离开') 会先命中标题文字，所以要排除问句和挽留按钮。
        按钮写法很多：放弃奖励离开 / 坚持退出 / 残忍离开 / 退出 / 离开 /
        确认退出…，都按配置里的词表来认，别只认前几种。
        """
        if screen is None:
            return None
        stay = [str(w) for w in (self.config.get('video_stay_words') or []) if w]
        words = [str(w) for w in (self.config.get('video_leave_words') or [])
                 if w and not any(s in w for s in stay)]
        pattern = '|'.join(re.escape(w) for w in words) if words else ''
        for n in screen.nodes:
            text = normalize(n.text)
            if not text or any(w in text for w in stay):
                continue
            if '?' in text or '？' in text or '吗' in text:
                continue
            if pattern and re.search(pattern, text):
                return n
        return None

    def _video_handle_leave_dialog(self, screen):
        """视频退出确认弹窗：逐级点掉「退出/离开」，直到回列表或没有弹窗。

        有的广告点一次 X 后连弹两级（「坚持退出」→「确认要离开吗」），只处理
        一级就撒手，后面那级没人点，脚本只能按返回键、退不掉就杀 APP 重启。
        这里连点几级，每点一次都读屏确认；一旦回到列表立即停手，不误伤。
        """
        if screen is None:
            return False
        tries = max(1, int(self.config.get('video_exit_dialog_tries', 3)))
        settle = float(self.config.get('exit_dialog_interval', 1.0))
        clicked = False
        for _ in range(tries):
            if self._at_task_list(screen):
                break
            if self._find(screen, VIDEO_LEAVE_DIALOG_RE) is None:
                break
            button = self._video_leave_button(screen)
            if button is None:
                break
            logger.info('视频退出确认弹窗，点「%s」', button.text)
            self.click(button)
            clicked = True
            self.control.wait(settle)
            screen = self._safe_read(screen)
        return clicked

    def _video_tap_close(self, screen):
        """点视频页的关闭入口（跳过 / 关闭广告 / 右上角几何 X）。返回是否点过。"""
        if screen is None:
            return False
        words = [str(w) for w in (self.config.get('video_close_words') or []) if w]
        pattern = '|'.join(re.escape(w) for w in words)
        node = self._find(screen, pattern) if pattern else None
        if node is not None:
            logger.info('视频页点关闭入口「%s」', node.text)
            self.click(node)
            return True
        # 页面自带「奖励将于N秒后发放 」横幅、X 是画上去的图标（控件树无文字），
        # 按右上角几何位置点它，避免这部分视频再也退不出去。
        if (self._find(screen, VIDEO_REWARD_COUNTDOWN_RE) is not None
                or self._is_ad_or_video_page(screen)):
            x = int(screen.width * float(self.config.get('video_close_x_ratio', 0.91)))
            y = int(screen.height * float(self.config.get('video_close_y_ratio', 0.08)))
            logger.info('视频页按右上角几何位置点 X (%d,%d)', x, y)
            self.click(TextNode('关闭视频', (x - 20, y - 20, x + 20, y + 20)))
            return True
        return False

    def _video_exit(self, screen):
        """看完视频后按人工步骤退出，尽快回到任务列表领奖。

        两条路（对应人工操作）：
        A) 手势返回 1 次 → 判定是否回列表；没回就快速再返回 2 次。
        B) 手势退不掉的页面：点 X / 跳过 关闭入口，弹出二级确认后点
           「放弃奖励离开」。看满时长后离开照样算完成，不点「抓住奖励机会」。
        """
        settle = float(self.config.get('video_back_settle', 0.8))
        fast_gap = float(self.config.get('video_fast_back_gap', 0.25))

        # 有些视频返回一次就直接弹二级确认框，先处理掉
        if self._video_handle_leave_dialog(screen):
            screen = self._safe_read(screen)
            if self._at_task_list(screen):
                return screen

        # A) 手势返回一次
        logger.info('视频看完，手势返回一次')
        self.device.go_back()
        self.control.wait(settle)
        screen = self._safe_read(screen)
        if self._at_task_list(screen):
            logger.info('视频返回一次已回到任务列表')
            return screen
        if self._video_handle_leave_dialog(screen):
            screen = self._safe_read(screen)
            if self._at_task_list(screen):
                return screen

        # B) 手势退不掉：优先点 X / 跳过，再点二级确认的「离开」
        if self._video_tap_close(screen):
            self.control.wait(settle)
            screen = self._safe_read(screen)
            if self._video_handle_leave_dialog(screen):
                screen = self._safe_read(screen)
            if self._at_task_list(screen):
                return screen

        # A2) 仍没回列表：快速返回两次，每次之间仍判定，回到列表就停手
        for _ in range(2):
            if self._at_task_list(screen):
                break
            self.device.go_back()
            self.control.wait(fast_gap)
            screen = self._safe_read(screen)
            if self._video_handle_leave_dialog(screen):
                screen = self._safe_read(screen)
            if self._at_task_list(screen):
                logger.info('快速返回后已回到任务列表')
                break
        return screen

    # ------------------------------------------------------------------
    # 恢复：框架的一次冷启动预算用完后，补一次强制重启客户端
    # ------------------------------------------------------------------
    def recover(self, cold_start=False):
        """导航恢复；仍失败时杀进程重开闲鱼再试一次。

        框架的 `_navigate_list` 只在「检测到桌面」时才冷启动应用。实际跑下来更
        常见的是页面停在闲鱼内部的 H5 死页：其实已经在应用里，但怎么退都回不到
        任务列表，导航把预算耗完就抛「未能自动恢复应用活动任务列表」。这里补一次
        真正的杀进程重开，把 H5 页面栈清干净再走一遍导航；还是不行才向上抛。
        """
        if cold_start:
            # 调用方明确要冷启动时，自己干净地重启一次，框架只负责导航。
            # 以前把 cold_start 原样传给框架：框架先重启一遍，失败后这里再
            # 重启一遍，单个任务白耗 40 多秒。
            self._restart_client()
            try:
                return super().recover(cold_start=False)
            except Exception as exc:
                # 浮层挡住「赚更多」时框架会逐层 go_back 退到桌面再判失败；
                # 此时页面可能已经稳定下来。最后再试一次：浮层常在这一轮自行
                # 消退，重读屏往往已经在落地页或任务列表，能捞回就别提前收工。
                return self._last_ditch_recover(exc)
        try:
            return super().recover(cold_start=False)
        except Exception as exc:
            if not self._restart_client():
                raise
            logger.warning('导航未能恢复，已强制重启闲鱼后重试：%s', exc)
            try:
                return super().recover(cold_start=False)
            except Exception as exc2:
                return self._last_ditch_recover(exc2)

    def _last_ditch_recover(self, exc):
        """导航判失败后的最后兜底：读当前屏，能进面板就进，否则原样抛出。"""
        package = self.origin or 'com.taobao.idlefish'
        try:
            screen = self.read(force_ocr=bool(self.config.get('ocr', False)))
        except Exception:
            raise exc
        if not (screen.package or '').startswith(package):
            raise exc
        try:
            if self._accept_list(screen):
                logger.info('导航虽判失败，但当前已在任务列表，继续执行')
                return screen
            if self.activity_landing(screen):
                resumed = self.enter_task_panel(screen)
                if resumed is not None and self._accept_list(resumed):
                    logger.info('导航虽判失败，但已重新打开任务面板，继续执行')
                    return resumed
        except Exception as rescued:
            logger.warning('最后兜底恢复出错：%s', rescued)
        raise exc

    def _restart_client(self):
        """回桌面 → force-stop → 重新启动。每轮只做一次。"""
        if getattr(self, '_client_restarted', False):
            return False
        self._client_restarted = True
        package = self.origin or 'com.taobao.idlefish'
        logger.warning('强制重启 %s：先回桌面再强停，避免下次启动恢复旧页面', package)
        try:
            self.device.go_home()
            self.control.wait(0.6)
            self.device.stop_app(package)
            self.control.wait(1.2)
            self.device.start_app(package)
            self.control.wait(float(self.config.get('restart_settle_seconds', 5.0)))
        except Exception as exc:
            logger.warning('强制重启闲鱼失败：%s', exc)
            return False
        return True

    def _resume_idle_fish(self, origin):
        """跨 App 返回时温和地把闲鱼切回前台。

        返回已确认在前台的屏幕；确实切不回来返回 None，让调用方决定冷启动。
        设备断流（offline/连接被拒）时原样抛出，交给上层有界恢复处理，
        不在断流状态下继续发 go_home/stop_app 把 adb 连接彻底搞断。
        """
        tries = max(1, int(self.config.get('cross_app_resume_tries', 3)))
        settle = float(self.config.get('resume_settle_seconds', 1.5))
        last = None
        for attempt in range(tries):
            self.control.check()
            try:
                self.device.resume_app(origin)
            except Exception as exc:
                if self._device_offline(exc):
                    raise
                last = exc
                self.control.wait(settle)
                continue
            self.control.wait(settle)
            try:
                resumed = self.read()
            except Exception as exc:
                if self._device_offline(exc):
                    raise
                last = exc
                continue
            if resumed is not None and (resumed.package or '').startswith(origin):
                logger.info('已将闲鱼切回前台（第 %d 次）', attempt + 1)
                return resumed
            last = None
        return None

    @staticmethod
    def _device_offline(exc=None):
        """异常文案里是否出现设备断流特征（offline / 连接被拒绝 / 元数据超时）。"""
        if exc is None:
            return False
        text = str(exc).lower()
        return any(word in text for word in ('offline', 'connection refused', 'device not found',
                                             '元数据超时', '未确认所选所选应用实例'))

    def _return_to_list(self, screen=None, tries=5, cold_start=False):
        """任务页结束后主动返回任务列表。

        广告/视频页先反复点页面内的退出按钮（二次确认也点掉）；小游戏容器交给
        _exit_game 处理。普通浏览/搜索页只做一次返回动作，等 1～2 秒读一屏，
        仍不在任务列表就直接恢复导航，不再一轮轮按返回键。
        """
        if screen is None:
            screen = self._safe_read()
        if screen is None:
            # 读屏完全不可用时按返回键只是盲退，直接交给恢复导航重开活动
            raise TaskUnavailable('读屏不可用，无法确认是否回到任务列表')
        # 跳转到其它 App 时（package 不是闲鱼），返回键只会退那个 App 直到桌面，
        # 回不到闲鱼；直接恢复（启动闲鱼 + 导航回任务列表），不傻按返回键。
        origin = self.origin or 'com.taobao.idlefish'
        if not (screen.package or '').startswith(origin):
            if self._at_task_list(screen):
                return screen
            # 跨 App 任务结束时闲鱼通常还在后台，把它切回前台即可，不必杀进程。
            # 只试一次、只等 1.5 秒对「去头条玩一玩」这类慢切换不够：刚跳到外部
            # App 时窗口/渲染常要几秒才就绪，一次没中就立刻 go_home+强停重启，
            # 白等二十多秒不说，界面看着就像脚本异常停住了。改成温和重试几次。
            try:
                screen = self._resume_idle_fish(origin)
            except Exception as exc:
                logger.warning('多次切回闲鱼仍未成功，改用冷启动：%s', exc)
                try:
                    return self.recover(cold_start=True)
                except Exception as exc2:
                    logger.warning('跨 App 返回闲鱼失败：%s', exc2)
                    return screen
            if screen is None:
                return self.recover(cold_start=True)
        # 视频/广告页：把连弹的退出确认一次点干净，再判断是否已经回到列表
        if self._is_ad_or_video_page(screen):
            screen = self._dismiss_exit_dialogs(screen)
            if self._at_task_list(screen):
                return screen
        back_tries = max(1, int(self.config.get('return_back_tries', 1)))
        settle = float(self.config.get('return_settle_seconds', 1.5))
        for _ in range(back_tries):
            if self._at_task_list(screen):
                return screen
            if self._is_game_container(screen):
                self._exit_game()
                screen = self.read()
                continue
            # 退出弹窗前面已点过一次；这里不再重复点，避免空耗（点不中就
            # 直接按返回/走恢复重进，终点都是重启）。
            # 已退到金币落地页（不在任务面板）：再退就越退越远，直接重开面板
            if self.activity_landing(screen):
                break
            # 页面内「返回」按钮优先，找不到才按系统返回键；两者只做一次，
            # 避免一次点按钮 + 一次返回键把页面退过头。
            label = str(self.config.get('browse_reward_button', '返回'))
            back = self._find(screen, re.escape(label), exact=True)
            if back is not None:
                logger.info('点击页面内「%s」返回任务列表', label)
                self.click(back)
            else:
                self.device.go_back()
            self.control.wait(settle)
            screen = self.read()
        if not self._at_task_list(screen):
            try:
                return self.recover(cold_start=cold_start)
            except Exception as exc:
                logger.warning('返回任务列表失败：%s', exc)
        return screen

    def _still_on_list(self, screen):
        """点击「去完成」后是否仍停在闲鱼任务列表（说明跳转根本没生效）。"""
        origin = self.origin or 'com.taobao.idlefish'
        if not (screen.package or '').startswith(origin):
            return False
        return self.is_task_list(screen.nodes)

    def _visit_done(self, task, screen):
        """回到任务列表后，该跳转任务是否已完成（行变成「领取奖励」/进度+1）。

        未安装目标 App 时，点「去完成」跳不过去、行仍是「去完成」，据此判定
        任务无法完成，交由调用方跳过；其余情况给进度刷新留几拍再下结论。
        """
        deadline = self.control.now() + int(self.config.get('visit_settle_seconds', 4))
        while self.control.now() < deadline:
            if self.is_task_list(screen.nodes):
                cur = next((t for t in self.parse_tasks(screen.nodes, screen.width, screen.height)
                            if t.title == task.title), None)
                if cur is not None and cur.current > task.current:
                    return True
                if cur is not None and cur.action in ('领取奖励', '立即领取'):
                    return True
                # 仍是「去完成」：等进度刷新，再读一次
                self.control.wait(1)
                screen = self.read()
                continue
            screen = self.read()
            self.control.wait(1)
        return False

    def _swipe_once(self, screen):
        """一次连续上滑；手势本身拉长，间隔交给调用方压到极小。"""
        width, height = screen.width, screen.height
        self.device.swipe(int(width * .5), int(height * .72),
                          int(width * .5), int(height * .46), duration=0.5)

    def _video_browse(self, seconds):
        """视频页不用滑动：等页面要求的秒数，再按人工步骤退出来领奖。

        人工是「点进去 → 等 15~20 秒 → 退出来领奖」。时长优先用页面自己的
        倒计时（「奖励将于14秒后发放」），读不到才用配置 video_seconds；
        这样页面只要求 14 秒时不用死等 20 秒，省下的都是每轮的纯等待。

        刚点完「去完成」时 H5 还没跳转，第一屏读到的往往仍是任务列表；
        必须确认真的进过视频页再开始算，否则视频一秒没播就被当成看完。
        """
        screen = self.read()
        default = int(seconds or self.config['browse']['video_seconds'])
        # 页面自带倒计时就按它来，并留一点缓冲，别卡在最后一秒退出
        match = re.search(VIDEO_REWARD_COUNTDOWN_RE, normalize(screen.text))
        desired = int(match.group(1)) if match else default
        if match:
            logger.info('视频页要求等待 %d 秒（页面倒计时）', desired)
        total = max(1, desired + int(self.config.get('browse_extra_seconds', 2)))
        self.update(message='浏览视频 %d 秒' % total)
        deadline = self.control.now() + total
        explicit = False
        entered = False
        while True:
            if screen.find(DONE_PROMPT_RE):
                explicit = True
                logger.info('检测到视频完成提示，提前结束浏览')
                break
            if self.is_task_list(screen.nodes):
                if entered:
                    # 已经离开过列表又回到列表：视频页结束
                    break
                if self.control.now() >= deadline:
                    logger.info('视频页 %.0f 秒内未打开，按超时结束', total)
                    break
                logger.info('视频页尚未打开，继续等待')
                self.control.wait(1)
                screen = self._safe_read(screen, force_ocr=bool(self.config['ocr']))
                continue
            entered = True
            # 进视频页后再读一次倒计时，页面可能这时才渲染出来
            if not match:
                match = re.search(VIDEO_REWARD_COUNTDOWN_RE, normalize(screen.text))
                if match:
                    desired = int(match.group(1))
                    total = max(1, desired + int(self.config.get('browse_extra_seconds', 2)))
                    deadline = self.control.now() + total
                    logger.info('视频页要求等待 %d 秒（页面倒计时）', desired)
            remaining = deadline - self.control.now()
            if remaining <= 0:
                break
            self.control.wait(min(2, max(.2, remaining)))
            screen = self._safe_read(screen, force_ocr=bool(self.config['ocr']))
        # 按人工步骤退出：手势返回1次→判定→快返2次；退不掉就 X/跳过→二级弹窗点离开
        screen = self._video_exit(screen)
        return screen, explicit

    # 真正的商品卡片都标「抵后价」（闲鱼币抵扣后实付价），用它锁定商品；
    # 运营活动卡（现金夺宝 / 大夺宝等）没有「抵后价」，自然被排除，不必再
    # 维护关键词黑名单（黑名单永远补不完）。兼容个别「到手价 / 券后价」写法。
    _GOODS_MARKERS = ('抵后价', '到手价', '券后价', '券后到手')

    def _goods_nodes(self, screen):
        """商品列表里可点的商品卡片：必须带「抵后价」等商品价签，非广告、非按钮。"""
        out = []
        for n in screen.nodes:
            t = normalize(n.text)
            if not any(m in t for m in self._GOODS_MARKERS):
                continue
            if '广告' in t:
                continue
            if re.fullmatch(ACTION_RE, t):
                continue
            cy = n.center[1]
            if not (screen.height * 0.18 < cy < screen.height * 0.85):
                continue
            out.append(n)
        return out

    @staticmethod
    def _is_detail_page(screen):
        """是否已进入单品详情页：详情页有「立即购买/我想要/收藏」等特征，列表页没有。"""
        return any(h in normalize(screen.text)
                   for h in ('立即购买', '我想要', '加入购物车', '收藏', '马上抢'))

    def _click_goods(self, screen, count):
        """[已停用] 点击指定频道好物类任务（用户要求不做，保留代码备查）。

        已从 execute 与 _classify 中摘除：该任务现归 trade 类型，由框架直接跳过。

        人工流程：点「去完成」进入商品流 → 点一个商品 → 直接返回（计数+1）
        → 找下一个（必要时下滑）→ 点 → 返回 … 循环直到点满 count 个。
        注意：列表里混杂广告，点了不计数，需排除带「广告」标记的条目；
        同一商品不重复点（按文本去重）。
        """
        clicked = set()           # 已点商品的去重标识（文本）
        done = 0
        empty = 0                 # 连续没找到可点商品的屏数（防空转）
        hard = int(self.config.get('max_task_seconds', 180))
        deadline = self.control.now() + hard
        while done < count and self.control.now() < deadline:
            screen = self.read()
            # 已回到任务列表（任务完成 / 意外退出）则停止
            if self.is_task_list(screen.nodes):
                break
            # 退过头落到闲鱼首页/账户页（带原生底部导航）：首页商品不带
            # 「抵后价」，在这里点不到有效计数，立即停止交给外层重进
            if self._has_bottom_nav(screen):
                logger.warning('已退出活动商品流（检测到主页底部导航），停止点击')
                break
            # 仍卡在商品详情页（返回没生效）：补一次返回再读，别在详情页
            # 的「猜你喜欢」里找「抵后价」
            if self._is_detail_page(screen):
                logger.info('仍在商品详情页，补一次返回')
                self.device.go_back()
                self.control.wait(1.5)
                screen = self.read()
                if self.is_task_list(screen.nodes) or self._has_bottom_nav(screen):
                    break
            goods = self._goods_nodes(screen)
            if len(goods) < 2:
                # 页面可能正在刷新（返回后 H5 重渲染有短暂空白），先等一拍再读
                self.control.wait(1.5)
                screen = self.read()
                goods = self._goods_nodes(screen)
            if len(goods) < 2:
                # 连续两屏都没有「抵后价」商品：多半已不在有效计数页，放弃
                empty += 1
                if empty >= 2:
                    logger.warning('连续 %d 屏未找到「抵后价」商品，停止点击', empty)
                    break
                self._swipe_once(screen)
                self.control.wait(1.5)
                continue
            empty = 0
            # 选一个尚未点过的（按文本去重，避免重复点同一商品；自上而下）
            target = None
            for g in sorted(goods, key=lambda n: n.center[1]):
                if normalize(g.text) and normalize(g.text) not in clicked:
                    target = g
                    break
            if target is None:
                # 本屏商品都已点过：下滑刷新
                self._swipe_once(screen)
                self.control.wait(1.2)
                continue
            clicked.add(normalize(target.text))
            self.update(message='点击商品 %d/%d' % (done + 1, count))
            logger.info('点击商品 %d/%d：%s', done + 1, count, target.bounds)
            self.click(target)
            self.control.wait(1.2)
            # 验证是否真的进入详情：列表页会有多个商品（价格节点≥2），
            # 进详情后只剩单品，数量骤减；仍在列表说明点错（非商品卡片）。
            s2 = self.read()
            if not self._is_detail_page(s2):
                # 没进详情（点到夺宝 / 筛选等），不计数，换下一个目标
                logger.warning('点击后未进入商品详情，可能点到非商品，跳过该位置：%s',
                               target.bounds)
                self._swipe_once(s2)   # 小幅下移，避免重复选到同一位置
                self.control.wait(1.0)
                continue
            # 已进入详情：点一个商品只返回一次即可回到商品流（活动列表），
            # 不循环连按，否则会一路退到闲鱼首页。
            self.device.go_back()
            self.control.wait(1.5)
            s3 = self.read()
            if self._is_detail_page(s3):
                # 详情页 H5 有中转层，返回一次还没退出来，再退一次
                self.device.go_back()
                self.control.wait(1.5)
                s3 = self.read()
            # 必须同时满足「不在详情页」且「有多个商品」才算回到商品流，
            # 详情页底部的「猜你喜欢」也带「抵后价」，只看数量会误判
            if not self._is_detail_page(s3) and len(self._goods_nodes(s3)) >= 2:
                done += 1
                continue
            # 返回后已不在商品流（退到了落地页 / 闲鱼主页）：在这里滑动找
            # 「抵后价」是纯空转，立即停止，交给外层重新进入任务
            logger.warning('返回后已不在商品流，停止点击（已点 %d/%d）', done + 1, count)
            break
        return self.read()

    def _search_task(self, screen):
        """搜索类任务（如「搜一搜推荐商品」）。

        点「去完成」进入搜索页后，光点推荐词是过不去的：闲鱼按「真的搜了商品
        并浏览」计数，实测连续点「淘宝精选」十几次，进度始终 0/1。这里改成和
        人工一致的做法：点开搜索框 → 输入关键词 → 提交搜索 → 滑动浏览结果。

        关键词取活动配置 task_search_keyword，留空时从公共词表
        task_search_keywords 随机抽一个。输入不可用时退回点推荐词，
        两者都确认不了就跳过，不在未搜索的页面上滑动。
        """
        s = self.read()
        submitted = False
        box = self._find(s, r'搜索|搜一搜|找闲置|搜你想不到')
        if box is not None:
            self.click(box)
            self.control.wait(0.8)
            s = self.read()
            submitted = self._input_search(s) or self._tap_suggestion(s)
        if not submitted:
            raise TaskUnavailable('未确认搜索入口，跳过当前搜索任务')
        # 浏览搜索结果：和普通浏览一样【连续不断】滑够时长。
        # 原来这里按「滑一次→等0.4秒→读一次屏」循环，而 DML OCR 读一屏要好几秒，
        # 读屏期间手是停的；闲鱼只算「持续滑动」的时间，一停就重新计时，实测
        # 滑了 15 秒只算 8 秒、任务失败。改成交给 _browse：中途完全不读屏，
        # 手势之间只留很短间隔，滑满时长再读一次确认，和已验证的浏览任务一致。
        s, explicit = self._browse(self.config['browse']['regular_seconds'])
        return s, explicit

    def _search_keyword(self):
        """搜索任务用的关键词：活动配置优先，其次公共词表随机抽取。"""
        configured = normalize(str(self.config.get('task_search_keyword') or ''))
        if configured:
            return configured
        pool = [normalize(str(w)) for w in (self.config.get('task_search_keywords') or [])]
        pool = [w for w in pool if w]
        return random.choice(pool) if pool else ''

    def _input_search(self, screen):
        """在搜索页输入关键词并提交搜索；输入不可用返回 False。"""
        keyword = self._search_keyword()
        if not keyword:
            return False
        try:
            self.device.input_text(keyword)
        except Exception as exc:
            logger.warning('搜索任务：输入关键词失败（%s），改用推荐词', exc)
            return False
        self.update(message='搜索：' + keyword)
        self.control.wait(1.2)
        later = self._safe_read(screen)
        submit = None
        if later is not None:
            submit = next((n for n in later.nodes
                           if normalize(n.text) == '搜索'
                           and n.center[1] < later.height * 0.2
                           and n.center[0] > later.width * 0.6), None)
        if submit is not None:
            self.click(submit)
        else:
            self.device.press('enter')
        self.control.wait(1.5)
        logger.info('搜索任务：输入关键词「%s」并提交搜索', keyword)
        return True

    def _tap_suggestion(self, screen):
        """输入不可用时，点搜索发现词云里的第一个推荐词。"""
        if not self._find(screen, r'搜索发现|热门搜索|猜你想搜|历史搜索'):
            return False
        # 排除词云标题本身（搜索发现 / 猜你想搜 / 历史搜索）和按钮文本。
        recs = [n for n in screen.nodes
                if 2 <= len(normalize(n.text)) <= 10
                and normalize(n.text) not in ('搜索发现', '热门搜索', '猜你想搜',
                                              '历史搜索', '清空')
                and screen.height * 0.25 < n.center[1] < screen.height * 0.72
                and not re.fullmatch(ACTION_RE, normalize(n.text))]
        if not recs:
            return False
        self.click(recs[0])
        self.control.wait(1.2)
        logger.info('搜索任务：点推荐词「%s」', normalize(recs[0].text))
        return True

    def _browse(self, seconds):
        """浏览页：必须不间断滑动满 N 秒才计时。

        闲鱼浏览任务按「页面持续滑动」计时，任何停顿（读屏、二次检测）都会让
        计时不连续、从而被判定为未完成。因此滑动期间**完全不读屏**，只靠时长
        覆盖；滑够时长后再读一次确认是否出现完成提示，必要时补滑一段。

        每次手势之间留 browse_swipe_interval（默认 0.2 秒）：手势过密会让商品
        瀑布流在十几秒内加载几十屏，控件树膨胀后读屏直接抛 StackOverflowError。
        收尾读屏失败时沿用最后一次成功的屏幕，按「没有完成提示」继续返回列表。
        """
        screen = self.read()
        desired = self._browse_seconds(screen, seconds)
        total = desired + int(self.config.get('browse_extra_seconds', 5))
        logger.info('浏览任务：页面要求 %d 秒，连续滑动 %d 秒', desired, total)
        interval = max(0.02, float(self.config.get('browse_swipe_interval', 0.2)))
        deadline = self.control.now() + total
        while self.control.now() < deadline:
            self._swipe_once(screen)
            # 手势之间只留短衔接间隔；不读屏，保证计时连续
            self.control.wait(interval)
        # 滑动结束后再读屏确认完成状态
        screen = self._safe_read(screen)
        explicit = bool(screen.find(DONE_PROMPT_RE))
        if not explicit:
            countdown = self._countdown(screen)
            if countdown is not None:
                # 页面仍有倒计时（极少触发）：补滑到倒计时结束
                logger.info('页面仍有倒计时 %d 秒，补滑', countdown)
                end = self.control.now() + countdown + 1
                while self.control.now() < end:
                    self._swipe_once(screen)
                    self.control.wait(interval)
                screen = self._safe_read(screen)
                explicit = bool(screen.find(DONE_PROMPT_RE))
        return screen, explicit

    @staticmethod
    def _browse_seconds(screen, fallback):
        """从页面文案里读「浏览20秒 / 滑动浏览20s」，读不到就用配置值。"""
        match = re.search(BROWSE_SECONDS_RE, normalize(screen.text))
        return int(match.group(1)) if match else int(fallback or 20)

    @staticmethod
    def _countdown(screen):
        """页面上的剩余秒数提示，没有就是 None。"""
        match = re.search(COUNTDOWN_RE, normalize(screen.text))
        return int(match.group(1)) if match else None

    # ------------------------------------------------------------------
    # 领奖后的通用弹窗
    # ------------------------------------------------------------------
    def _click_text(self, screen, pattern, exact=False):
        node = self._find(screen, pattern, exact=exact)
        if node is not None:
            self.click(node)
            return True
        return False

    def _dismiss_popups(self, screen):
        if screen is None:
            return False
        if self._click_text(screen, '立即领取|开心收下|全部收下|收下礼物'):
            self.control.wait(1.5)
            self._click_text(self.read(), '关闭')
            return True
        for btn in self.config['popup_buttons']:
            if btn in ('关闭', '返回'):
                continue
            if self._click_text(screen, re.escape(btn)):
                self.control.wait(1.2)
                return True
        return False
