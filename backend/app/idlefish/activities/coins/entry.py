"""闲鱼币大富翁的入口与任务面板识别（对齐 MiaoTask 导航钩子）。

框架的 Navigator._navigate_list 通过以下钩子自动完成：
首页 → 闲鱼币入口 → 大富翁落地页 → 打开任务面板 → 回到面板（恢复）。
这些钩子全部基于 UI 树 / OCR 文本匹配，不写死坐标；具体文案放在 config.json。
"""
import logging
import re

from backend.core.models import TextNode, normalize

logger = logging.getLogger('backend.execution')


class EntryPolicy:
    # ---- 基础观察 ----
    @staticmethod
    def _find(screen, pattern, exact=False):
        return screen.find(pattern, exact=exact)

    # ---- 是否在 Android 桌面 / 启动页 ----
    @staticmethod
    def launcher_screen(screen):
        package = (screen.package or '').lower()
        return (not package.startswith('com.taobao.idlefish')
                and ('launcher' in package or package.endswith('.home')))

    @staticmethod
    def launch_screen(screen):
        # 闲鱼无 splash 特征；返回 False 让导航直接进入活动识别。
        return False

    # ---- 是否在闲鱼首页（底部导航） ----
    def homepage_visible(self, screen):
        labels = self.config.get('homepage_labels', ['首页', '我的'])
        return all(any(normalize(n.text) == label and n.center[1] > screen.height * .8
                       for n in screen.nodes)
                   for label in labels)

    # ---- 原生底部导航只在首页/账户页出现，活动页是 H5 容器不带它 ----
    @staticmethod
    def _has_bottom_nav(screen):
        """用来区分「账户页宽 banner」和「活动页前进按钮」，避免错页点击。"""
        return any(normalize(n.text) in ('首页', '我的', '消息', '卖闲置', '会玩')
                   and n.center[1] > screen.height * .8 for n in screen.nodes)

    # ---- 「前进」大按钮：带步数文案的半屏宽节点 ----
    @staticmethod
    def _step_button(screen):
        width, height = screen.width, screen.height
        candidates = []
        for n in screen.nodes:
            left, top, right, bottom = n.bounds
            if right - left < width * .35 or right - left > width * .95:
                continue
            if bottom - top < height * .02 or bottom - top > height * .16:
                continue
            if left < width * .08 or right > width * .95:
                continue
            candidates.append(n)
        if not candidates:
            return None
        # 只认带步数文案的宽节点（「再走36步完成1圈 14步」/「13步」）。
        # 视频流、商品浮层里也有半屏宽的卡片，页面加载中途还有无文案占位容器，
        # 不加这条会被当成「前进」按钮，进而推算出完全错误的入口坐标。
        stepped = [n for n in candidates if '步' in normalize(n.text)]
        if not stepped:
            return None
        return max(stepped, key=lambda n: n.bounds[2] - n.bounds[0])

    # ---- 是否已进入闲鱼币大富翁落地页 ----
    def activity_landing(self, screen):
        if self._has_bottom_nav(screen):
            # 账户页/首页有原生底部导航，那不是活动页（即便里面有宽 banner）
            return False
        # 任务面板已打开时不算落地页：面板顶部的引导卡片里也带「再走N步完成1圈」，
        # 只按文案判定会把面板误认成落地页，接着去点「赚更多」并按返回键，
        # 结果把面板整层退掉、还要多跑一轮恢复导航。面板特征优先于文案。
        if self.task_panel_visible(screen):
            return False
        if self._step_button(screen) is not None:
            return True
        return bool(self._find(screen, self.config['landing_regex']))

    # ---- 任务面板是否已成为可见的 sheet（标题或列表标记出现） ----
    def task_panel_visible(self, screen):
        if self._find(screen, self.config['panel_heading_regex']):
            return True
        return self.is_task_list(screen.nodes)

    # ---- 面板仍在加载 / 已展开但任务行未就绪 ----
    def panel_loading(self, screen):
        return bool(self._find(screen, self.config['panel_loading_regex']))

    # ---- 等「前进」按钮在屏幕上静止 ----
    def _wait_settle(self, step):
        """低端机动画慢、卡顿，按钮会先在偏上位置渲染、再滑到底部，中途点下去
        会落在空白处（实测点到 969,1155，面板打不开）。所以按「连续多次读数
        一模一样」判断静止；超时也照点最后位置，避免整轮导航重来。
        """
        bounds = tuple(step.bounds)
        samples = int(self.config.get('entry_settle_samples', 3))
        stable = 1
        self.control.wait(float(self.config.get('entry_settle_min', 1.2)))
        timeout = float(self.config.get('entry_settle_timeout', 12))
        deadline = self.control.now() + timeout
        while True:
            current = self._step_button(self.read(force_ocr=False))
            if current is None:
                return None
            latest = tuple(current.bounds)
            if all(abs(a - b) <= 2 for a, b in zip(bounds, latest)):
                stable += 1
                if stable >= samples:
                    return current
            else:
                logger.info('「前进」按钮位置未稳定（%s→%s），继续等待', bounds, latest)
                bounds = latest
                stable = 1
            if self.control.now() >= deadline:
                logger.info('「前进」按钮 %.0f 秒内未静止，按当前位置点击', timeout)
                return current
            self.control.wait(float(self.config.get('entry_settle_interval', 0.35)))

    @staticmethod
    def _geometry_entry(step):
        """按「前进」按钮的几何关系推算右上角圆形入口的中心。"""
        _, top, right, bottom = step.bounds
        height = bottom - top
        if height <= 0:
            return None
        side = max(int(height * 0.78), 40)
        cx = right + int(height * 0.03) + side // 2
        cy = bottom - side // 2 + int(height * 0.04)
        half = side // 2
        return TextNode('赚更多', (cx - half, cy - half, cx + half, cy + half), 'anchor')

    # ---- 几何锚点失效时的局部 OCR 兜底（只识别按钮带） ----
    def _visual_entry(self, screen, force=False):
        if not self.config.get('ocr_fallback', True):
            return None
        # 只在闲鱼币大富翁落地页做 OCR 兜底；执行任务途中停留的其它页面
        # （商品页、详情页、小游戏页…）按钮带里本来就没有入口，
        # 在那里识别纯属空跑，还会刷「未识别到赚更多」日志。
        if not self.activity_landing(screen):
            return None
        # 页面还没渲染完时 OCR 只会空跑，节流到每 2 秒最多一次；
        # 浮层重试场景 force=True 跳过节流，确保真的重算一次入口。
        now = self.control.now()
        if not force and now - getattr(self, '_visual_entry_at', 0) < 2:
            return None
        self._visual_entry_at = now
        reader = getattr(self, 'reader', None)
        device = getattr(self, 'device', None)
        # enrich_regions 是较新框架的本地 OCR 接口；不存在的版本直接跳过兜底
        if reader is None or device is None or not hasattr(reader, 'enrich_regions'):
            return None
        # 先排除「按钮只是还没画出来」：给它几拍时间，一旦出现就交回几何路径
        for _ in range(2):
            self.control.wait(0.4)
            step = self._step_button(self.read(force_ocr=False))
            if step is not None:
                logger.info('「前进」按钮已渲染，改用几何锚点')
                step = self._wait_settle(step)
                return self._geometry_entry(step) if step is not None else None
        label = normalize(self.config.get('entry_ocr_label', '赚更多'))
        # 手机按按钮带识别；平板/横屏/不同分辨率的布局差异很大，按钮带可能
        # 盖不到入口位置（实测平板 720×1080 上「赚更多」在屏高 81% 处），
        # 未命中时再全屏扫一次，做到分辨率无关
        bands = [tuple(self.config.get('entry_ocr_band', [0.45, 0.75]))]
        if self.config.get('entry_ocr_fullscreen', True):
            bands.append((0.0, 1.0))
        target = None
        for top_ratio, bottom_ratio in bands:
            region = (0, int(screen.height * top_ratio),
                      screen.width, int(screen.height * bottom_ratio))
            try:
                image = device.screenshot(max_age=.15).convert('RGB')
                enabled = getattr(reader, 'ocr_enabled', False)
                reader.ocr_enabled = True
                reader.last_image, reader.last_screen = image, screen
                try:
                    enriched = reader.enrich_regions(screen, [region])
                finally:
                    reader.ocr_enabled = enabled
            except Exception as exc:  # 截图/接口差异都只影响兜底，不影响主流程
                logger.info('入口 OCR 兜底失败：%s', exc)
                return None
            hits = [n for n in enriched.nodes
                    if getattr(n, 'source', '') == 'ocr-region'
                    and label in normalize(n.text)]
            if hits:
                # 页面上可能有多个「赚更多」文案，真正的入口在页面右下角，
                # 取最靠下的那个（同高度取更靠右的）
                target = max(hits, key=lambda n: (n.center[1], n.center[0]))
                break
        if target is None:
            # 最后兜底：按屏幕比例点「赚更多」的常见位置（竖屏自适应布局下
            # 相对位置基本稳定），任何分辨率都能算出坐标
            point = self.config.get('entry_point_fallback')
            if point:
                cx = int(screen.width * float(point[0]))
                cy = int(screen.height * float(point[1]))
                logger.info('入口 OCR 未命中，使用比例兜底坐标 (%d,%d)', cx, cy)
                return TextNode('赚更多', (cx - 24, cy - 16, cx + 24, cy + 16), 'ratio')
            logger.info('入口 OCR 兜底：未识别到「%s」', label)
            return None
        cx, cy = target.center
        # 文字标签落在圆形按钮下半部，按钮中心在它上方约 2.4% 屏高
        cy -= int(screen.height * float(self.config.get('entry_ocr_offset', 0.024)))
        logger.info('入口 OCR 兜底命中：%s → (%d,%d)', target.bounds, cx, cy)
        return TextNode('赚更多', (cx - 24, cy - 16, cx + 24, cy + 16), 'ocr')

    # ---- 从大富翁落地页找到「打开任务面板」的入口控件「赚更多」 ----
    # 落地页底部一排从左到右是三个控件：
    #   [1分兑换] [前进(mapStepBtn: 再走N步完成1圈)] [赚更多 / 领]
    # 最后一个圆形按钮同时承载「赚更多」和「领」两个入口：
    #   - 「领」有文本，只占该按钮右上角；
    #   - 「赚更多」是画布绘制，控件树里没有节点。
    # 首选以「前进」按钮为锚点做几何推算，零额外耗时；找不到才退回局部 OCR。
    def task_panel_entry(self, screen):
        if self.task_panel_visible(screen):
            return None
        if self._has_bottom_nav(screen):
            # 还在首页/账户页（带原生底部导航），不是活动页，别在错页点
            return None
        labels = self.config.get('task_entry_labels', ['赚更多'])
        node = self._find(screen, '|'.join(re.escape(normalize(x)) for x in labels))
        if node is not None:
            return node
        step = self._step_button(screen)
        if step is None or self.config.get('force_ocr_entry'):
            # force_ocr_entry 仅用于新机型验证：强制走一次 OCR 路径
            entry = self._visual_entry(screen)
            if entry is not None:
                return entry
            if step is None:
                return None
        step = self._wait_settle(step)
        if step is None:
            return None
        return self._geometry_entry(step)

    # ---- 找到「闲鱼币」活动入口：首页底部「我的」→ 账户页「闲鱼币」 ----
    def activity_entry(self, screen, homepage=False):
        labels = [normalize(x) for x in self.config.get('entry_labels', ['闲鱼币'])]

        def coin_of(scr):
            return next((n for n in scr.nodes
                         if normalize(n.text) in labels
                         and 0 < n.bounds[2] - n.bounds[0]
                         and n.bounds[3] - n.bounds[1] < scr.height * .12), None)

        coin = coin_of(screen)
        if coin is not None:
            return coin
        # 首页底部「我的」标签 → 进入账户页后再找「闲鱼币」
        my_tab = next((n for n in screen.nodes
                       if normalize(n.text) == '我的'
                       and n.center[1] > screen.height * .8
                       and 0 < n.bounds[2] - n.bounds[0]), None)
        if my_tab is None:
            return None
        self.click(my_tab)
        for _ in range(4):
            self.control.wait(0.8)
            scr = self.read(force_ocr=bool(self.config['ocr']))
            coin = coin_of(scr)
            if coin is not None:
                return coin
        return None

    # ---- 官方活动入口（闲鱼无独立官方卡片，返回 None） ----
    @staticmethod
    def official_activity_entry(screen):
        return None

    # ---- 不可用落地页（如误入小程序），交由导航逐层返回 ----
    @staticmethod
    def unavailable_landing(screen):
        return False

    # ---- 打开任务面板（若已打开直接返回） ----
    def open_task_panel(self, screen):
        if self.is_task_list(screen.nodes):
            return True
        node = self.task_panel_entry(screen)
        if node:
            self.click(node)
            return True
        return False

    # ---- 确认并等待任务面板就绪（框架在导航中调用） ----
    # 点一次「赚更多」，然后每秒核验一次是否进入任务列表，最长等 10 秒。
    # 不连点：手机卡顿时面板可能 3～10 秒才渲染，此时「赚更多」原来的位置
    # 已经被弹出面板上的其它控件（例如小游戏入口）占据，再点一次就会误进游戏。
    # 例外：10 秒后仍确认「面板没出现，且页面还停在活动落地页」，说明这一下
    # 点在了页面加载中途的过渡位置，允许重算坐标再点一次（最多一次）。
    # ---- 活动页运营弹窗（限时惊喜等）会挡住「赚更多」入口 ----
    _OVERLAY_CLOSE_RE = r'^(×|✕|x|X|关闭|跳过|以后再说|残忍拒绝)$'

    def _close_overlay(self, screen):
        """关掉当前屏幕上所有能识别到的浮层，返回是否点到。

        同屏常叠着运营弹窗和步数引导卡，原来一次只关一个、调用方再读一遍屏
        重新找，往往连读三四次。这里在一屏上把能点的一次点完，只等一次。
        """
        closed = False
        for n in screen.nodes:
            t = normalize(n.text)
            if t and re.fullmatch(self._OVERLAY_CLOSE_RE, t):
                logger.info('检测到活动弹窗浮层，点击「%s」关闭', t)
                self.click(n)
                closed = True
        # 「前进获得闲鱼币」引导浮层是画布绘制的卡片，关闭键既不在控件树里，
        # OCR 也常把它漏成空节点。按标题定位，点标题同一行最右侧（关闭键固定
        # 贴在卡片右上角），当作「×」的兜底；同屏多个卡片一次点完。
        guide = str(self.config.get('guide_title_regex') or '')
        if guide:
            pattern = re.compile(guide)
            for title in (n for n in screen.nodes if pattern.search(normalize(n.text))):
                y = title.center[1]
                if y > screen.height * 0.5:
                    # 引导卡片固定在屏幕上半部；标题跑到下半屏多半是 OCR 把
                    # 任务行读错了，照着点会误触「去完成」按钮
                    logger.debug('引导浮层标题位置异常（y=%d），忽略本次点击', y)
                    continue
                x = int(screen.width * float(self.config.get('guide_close_x', 0.88)))
                logger.info('检测到步数引导浮层，按标题位置点击关闭 (%d,%d)', x, y)
                self.click(TextNode('关闭浮层', (x - 24, y - 16, x + 24, y + 16)))
                closed = True
        if closed:
            self.control.wait(1.0)
        return closed

    def enter_task_panel(self, screen, expected_activity=False):
        panel_wait = self.config.get('panel_open_timeout', 10)
        deadline = self.control.now() + self.config['page_timeout']
        tapped_at = None
        retried = False
        while True:
            self.guard(screen)
            if self._accept_list(screen):
                if tapped_at is not None:
                    logger.info('任务列表已就绪，耗时 %.1f 秒', self.control.now() - tapped_at)
                return screen
            if not screen.package.startswith('com.taobao.idlefish'):
                return screen
            panel_loading = self.task_panel_visible(screen) or bool(self.panel_loading(screen))
            if not panel_loading and self.unavailable_landing(screen):
                return screen
            if self.control.now() >= deadline:
                return screen
            if tapped_at is not None:
                # 已点过：轮询等待面板渲染，期间不重复点击
                if self.control.now() - tapped_at < panel_wait:
                    self.control.wait(1.0)
                    screen = self.read(force_ocr=bool(self.config['ocr']))
                    continue
                if (not retried and not self.task_panel_visible(screen)
                        and self.activity_landing(screen)):
                    retried = True
                    # 点了「赚更多」但面板没出现且仍在落地页：多半是运营浮层挡了
                    # 点击。绝不按系统返回键——落地页是 H5，返回键会把整个活动页
                    # 退掉，框架随后「逐层返回」一路 go_back 到桌面，冷启动预算
                    # 用完后直接判失败提前收工（实测「去头条玩一玩」后中招）。
                    # 关不掉就留在落地页等浮层自行消退，然后在这里直接重算坐标
                    # 并把「赚更多」再点一次——成功那轮正是靠这一次点进去的。
                    # 绝不调用会点「我的」标签的重进逻辑（会让首页/我的来回跳）。
                    if not self._close_overlay(screen):
                        logger.info('弹窗无关闭按钮，留在落地页等待浮层消退，不按返回键')
                        self.control.wait(float(self.config.get('overlay_wait_seconds', 2.0)))
                    screen = self.read(force_ocr=bool(self.config['ocr']))
                    logger.info('关闭浮层后重算入口坐标，重试一次')
                    # 重算后当场再点一次入口，不把「等待」交回外层循环：
                    # 外层在 expected_activity=False（冷启动恢复）时找不到入口会
                    # 立刻 return 失败，触发框架 step_back 退到桌面。
                    if not self._accept_list(screen) and self.activity_landing(screen):
                        # 浮层会把「赚更多」的几何锚点顶歪，强制走 OCR 全屏兜底
                        # （含固定比例坐标），比几何推算更能点到真入口。
                        retry_entry = self._visual_entry(screen, force=True) or self.task_panel_entry(screen)
                        if retry_entry is not None:
                            logger.info('重试点击任务面板入口：%s | %s',
                                        retry_entry.text, retry_entry.bounds)
                            self.click(retry_entry)
                            tapped_at = self.control.now()
                            self.control.wait(0.4)
                            screen = self.read(force_ocr=bool(self.config['ocr']))
                            continue
                    # 浮层还没退、这次没找到可靠入口：再给它一点时间并重读，
                    # 保持在落地页内处理，绝不 return 失败给框架的逐层返回。
                    self.control.wait(float(self.config.get('overlay_wait_seconds', 2.0)))
                    screen = self.read(force_ocr=bool(self.config['ocr']))
                    continue
                logger.warning('点击「赚更多」后 %.0f 秒仍未出现任务列表，判定失败', panel_wait)
                return screen
            if not panel_loading and self._close_overlay(screen):
                # 落地页被运营弹窗（限时惊喜等）遮挡：先点「×」关掉再找入口，
                # 避免「赚更多」点在弹窗上无效
                screen = self.read(force_ocr=bool(self.config['ocr']))
                continue
            target = None if panel_loading else self.task_panel_entry(screen)
            if target:
                self.update(message='打开任务面板')
                logger.info('任务面板入口点击：%s | %s', target.text, target.bounds)
                self.click(target)
                tapped_at = self.control.now()
                self.control.wait(0.4)
                screen = self.read(force_ocr=bool(self.config['ocr']))
                continue
            if not (panel_loading or expected_activity):
                return screen
            self.control.wait(.4)
            screen = self.read(force_ocr=bool(self.config['ocr']))

    # ---- 当前屏幕是否为任务列表（被 _accept_list 与循环共同使用） ----
    def _accept_list(self, screen):
        # 只要具备任务列表特征（右侧两个以上动作按钮 / 累计奖励卡片）就算到位。
        # 不再强依赖 parse_tasks：面板刚展开时任务行可能还在渲染，过早判空会让
        # 导航反复重试，甚至把已经打开的面板又退回去。
        self.assert_instance(screen)
        if (self.is_task_list(screen.nodes)
                and screen.package.startswith('com.taobao.idlefish')):
            self.origin = screen.package
            return True
        return False
