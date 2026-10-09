"""Stable leaf identifiers; brands and specifications are item attributes."""
from __future__ import annotations

VERSION = 2
# Order is intentional: specific accessories precede the devices they fit.
RULES = [
    ('accessory.phone_case', '配件/保护/手机壳', r'手机壳|保护壳|防摔壳|(?:iPhone|苹果|三星|小米).{0,16}(?:壳|边框套)'),
    ('accessory.film', '配件/保护/贴膜', r'钢化膜|贴膜|屏幕膜'),
    ('accessory.stand', '配件/支撑/支架', r'支架|显示器臂|升降臂'),
    ('accessory.charger', '配件/供电/充电器', r'充电头|充电器|氮化镓|GaN\b'),
    ('accessory.powerbank', '配件/供电/充电宝', r'充电宝|移动电源'),
    ('accessory.cable', '配件/连接/线材', r'数据线|充电线|雷电线|HDMI线|DP线|音频线|网线'),
    ('accessory.dock', '配件/连接/扩展坞', r'扩展坞|拓展坞|雷电坞|USB\s*HUB'),
    ('accessory.stylus', '配件/输入/手写笔', r'Pencil|手写笔|触控笔'),
    ('accessory.battery', '配件/供电/替换电池', r'替换电池|备用电池|原装电池'),
    ('component.gpu', '电脑配件/计算/显卡', r'显卡|\b(?:RTX|GTX|RX)\s*\d{3,4}[A-Za-z]{0,5}|(?<!\d)(?:[2345]0[56789]0|[2345]0[56789]0\s*ti)(?!\d)'),
    ('component.cpu', '电脑配件/计算/CPU', r'CPU|处理器|Ryzen|锐龙|酷睿|(?<![A-Za-z0-9])[ir][3579][ -]?\d{4,5}[A-Za-z]{0,3}|(?<![A-Za-z0-9])\d{4,5}(?:X3D|KF|KS|K|F|X|HX|H|U)(?![A-Za-z0-9])'),
    ('component.mainboard', '电脑配件/平台/主板', r'主板|\b[ABXZH][6789]\d0[MIE-]?\b'),
    ('component.cpu_board_bundle', '电脑配件/平台/板U套装', r'板\s*[uU]|板U套|主板.{0,8}CPU.{0,8}套'),
    ('component.memory', '电脑配件/内存/形态未注明', r'内存条|(?<!显)内存|DDR[345]'),
    ('component.memory_desktop', '电脑配件/内存/台式内存', r'台式(?:机)?\s*(?:专用|用)?\s*内存|\bUDIMM\b'),
    ('component.memory_laptop', '电脑配件/内存/笔记本内存', r'笔记本\s*(?:拆机|专用|用)?\s*(?:内存|DDR[345])|SO[ -]?DIMM'),
    ('component.memory_ecc', '电脑配件/内存/ECC服务器内存', r'ECC|RDIMM|服务器内存'),
    ('component.psu', '电脑配件/供电/电源', r'电源|\b[AS]TX\s*\d|\bSFX\b'),
    ('component.case', '电脑配件/机箱/机箱', r'机箱|乔思伯|迎广'),
    ('component.air_cooler', '电脑配件/散热/风冷散热器', r'风冷|散热器|猫头鹰|利民|阿萨辛'),
    ('component.aio', '电脑配件/散热/一体式水冷', r'一体.{0,4}水冷|水冷|\bAIO\b'),
    ('component.custom_loop', '电脑配件/散热/分体水冷部件', r'分体水冷|冷头|冷排|水泵|水箱'),
    ('component.fan', '电脑配件/散热/风扇', r'风扇|\bNF-[AF]\d'),
    ('component.nic', '电脑配件/扩展/网卡', r'网卡|\b[IX][2357]\d0-[Tt]\d'),
    ('component.capture', '电脑配件/扩展/采集卡', r'采集卡|视频采集'),
    ('component.controller', '电脑配件/扩展/阵列与转接卡', r'阵列卡|HBA|转接卡|SAS卡'),
    ('storage.ssd_nvme', '存储/固态硬盘/NVMe SSD', r'NVMe|PCIe.{0,8}(?:固态|SSD)|(?:固态|SSD).{0,8}PCIe|\b(?:980|990|970)\s*(?:PRO|EVO)|SN[578]\d0'),
    ('storage.ssd_sata', '存储/固态硬盘/SATA SSD', r'SATA.{0,12}(?:固态|SSD)|(?:固态|SSD).{0,12}SATA|\b8[678]0\s*(?:PRO|EVO|QVO)'),
    ('storage.ssd', '存储/固态硬盘/接口未注明', r'固态|\bSSD\b|M\.2\s*(?:硬盘|SSD)'),
    ('storage.hdd', '存储/机械硬盘/机械硬盘', r'机械硬盘|\bHDD\b|西数.{0,4}盘|希捷|酷狼|酷鱼|银河.{0,8}[Tt]'),
    ('storage.drive', '存储/硬盘/类型未注明', r'硬盘'),
    ('storage.external', '存储/便携存储/移动硬盘', r'移动硬盘|移动固态|\bPSSD\b'),
    ('storage.card', '存储/便携存储/存储卡', r'TF卡|SD卡|存储卡|CFexpress|红金.{0,4}卡'),
    ('storage.usb', '存储/便携存储/U盘', r'U盘|优盘'),
    ('storage.enclosure', '存储/扩展/硬盘盒与柜', r'硬盘盒|硬盘柜|磁盘柜'),
    ('storage.nas', '存储/网络存储/NAS整机', r'NAS|群晖|威联通|绿联.{0,5}私有云|极空间'),
    ('network.router_soft', '网络/路由/软路由', r'软路由|多网口.{0,6}(?:主机|工控)|[四六248]网口.{0,8}(?:主机|N\d)'),
    ('network.router', '网络/路由/无线路由器', r'路由器|路由|(?<![A-Za-z0-9])(?:AX[135679]|BE[1369])\d{2,4}(?![A-Za-z0-9])'),
    ('network.switch', '网络/交换/交换机', r'交换机|PoE交换'),
    ('network.ap', '网络/无线/无线AP', r'无线AP|吸顶AP|面板AP|\bUnifi\b|\bEAP\d'),
    ('network.modem', '网络/接入/光猫', r'光猫|光调制解调'),
    ('network.cpe', '网络/接入/蜂窝CPE与随身WiFi', r'\bCPE\b|随身\s*Wi-?Fi|移动热点'),
    ('network.optics', '网络/配件/光模块与光纤', r'光模块|光纤|SFP\+?|QSFP'),
    ('display.monitor', '显示/桌面/显示器', r'显示器|\b[UP]\d{4}[A-Z]{0,3}\b'),
    ('display.portable', '显示/桌面/便携显示器', r'便携屏|便携显示器'),
    ('display.tv', '显示/家庭/电视', r'电视|TV\b|OLED.{0,8}(?:55|65|75|85)'),
    ('display.projector', '显示/家庭/投影仪', r'投影仪|投影机|极米|坚果.{0,4}投影'),
    ('input.keyboard', '外设/输入/键盘', r'键盘|Magic\s*Keyboard|HHKB|Realforce|Wooting|泰坦精英|臻世|STRIX\s*SCOPE|\bT87\b'),
    ('input.keycaps', '外设/输入/键帽与轴体', r'键帽|轴体|键盘套件'),
    ('input.mouse', '外设/输入/鼠标', r'鼠标|\bGPW\b|狗屁王|毒蝰|贱驴|\bRS2\b'),
    ('input.touchpad', '外设/输入/独立触控板', r'触控板|Trackpad'),
    ('input.mousepad', '外设/输入/鼠标垫', r'鼠标垫'),
    ('input.tablet', '外设/创作/数位板', r'数位板|数位屏|Wacom'),
    ('input.webcam', '外设/视频/摄像头', r'摄像头|Webcam'),
    ('office.printer', '办公/输出/打印与扫描设备', r'打印机|扫描仪|复印机'),
    ('office.ups', '办公/供电/UPS', r'\bUPS\b|不间断电源'),
    ('phone.apple', '手机/智能手机/苹果', r'iPhone|苹果\s*\d{1,2}|(?<![A-Za-z0-9])1[2-6]\s*(?:pm|pro\s*max)(?![A-Za-z0-9])'),
    ('phone.android', '手机/智能手机/安卓', r'Mate(?!Pad|Book)\s*\d|Pura\s*\d|Galaxy|三星\s*S\d|小米\s*\d|红米|Redmi(?!Book)|iQOO|OnePlus|一加|OPPO(?!\s*Pad)|vivo|荣耀(?!\s*(?:平板|笔记本))'),
    ('phone.feature', '手机/其他/功能机', r'功能机|老人机|诺基亚'),
    ('phone.unspecified', '手机/智能手机/品牌未注明', r'手机|(?<![A-Za-z0-9])1[7-9]\s*(?:pm|pro\s*max)(?![A-Za-z0-9])'),
    ('tablet.ipad', '平板/通用平板/iPad', r'iPad'),
    ('tablet.android', '平板/通用平板/安卓平板', r'MatePad|小米平板|安卓平板|OPPO\s*Pad|Vivo\s*Pad|荣耀平板|Galaxy\s*Tab'),
    ('tablet.ereader', '平板/阅读/电子书阅读器', r'Kindle|电子书|墨水屏|文石|掌阅'),
    ('tablet.other', '平板/通用平板/类型未注明', r'平板'),
    ('computer.laptop', '电脑整机/便携/笔记本', r'笔记本|MacBook|MacAir|\bMB[AP]\b|ThinkPad|MateBook|MagicBook|RedmiBook|Surface|\bXPS\b|游戏本|轻薄本|拯救者|幻\s*1[456]'),
    ('computer.mini', '电脑整机/桌面/小主机', r'小主机|迷你主机|Mac\s*(?:mini|studio)|\bNUC\b|nuc\s*\d|准系统|工控机|\bN[135]\d{2,3}\b'),
    ('computer.desktop', '电脑整机/桌面/台式整机', r'整机|台式|主机|攒机|整套电脑'),
    ('computer.aio', '电脑整机/桌面/一体机', r'iMac|一体机'),
    ('computer.server', '电脑整机/专业/服务器与工作站', r'服务器|工作站|PowerEdge'),
    ('computer.sbc', '电脑整机/开发/单板计算机', r'树莓派|Raspberry|香橙派|开发板'),
    ('game.handheld', '游戏/设备/掌机', r'Steam\s*Deck|掌机|ROG\s*Ally|Legion\s*Go'),
    ('game.console', '游戏/设备/家用游戏机', r'PS[345]|PlayStation|Xbox|任天堂|Nintendo|Switch|NS2'),
    ('game.vr', '游戏/设备/VR与AR设备', r'\bVR\b|Quest|Pico\s*\d|Vision\s*Pro|AR眼镜'),
    ('game.controller', '游戏/外设/手柄与方向盘', r'手柄|游戏方向盘|街机摇杆'),
    ('game.media', '游戏/实体内容/卡带与光盘', r'卡带|游戏光盘|游戏碟'),
    ('audio.tws', '音频/耳机/真无线耳机', r'AirPods(?!\s*Max)|FreeBuds|Buds|真无线|\bTWS\b'),
    ('audio.inear', '音频/耳机/有线入耳耳机', r'入耳|耳塞|\bIE\s*[269]00|\bIEM\b'),
    ('audio.overear', '音频/耳机/头戴耳机', r'头戴|AirPods\s*Max|\bHD\s*[568]\d{2}|\bWH-?1000'),
    ('audio.headphone', '音频/耳机/类型未注明', r'耳机'),
    ('audio.speaker', '音频/音箱/桌面与HiFi音箱', r'音箱|音响|监听箱'),
    ('audio.portable', '音频/音箱/便携与智能音箱', r'蓝牙音箱|智能音箱|HomePod'),
    ('audio.dac', '音频/前端/DAC与耳放', r'解码|耳放|\bDAC\b|fosi|飞傲.{0,4}K\d|\bK7\b'),
    ('audio.amp', '音频/前端/功放与接收机', r'功放|AV接收'),
    ('audio.player', '音频/前端/播放器', r'播放器|Walkman|随身听'),
    ('audio.mic', '音频/录制/麦克风与声卡', r'麦克风|话筒|声卡|录音笔'),
    ('camera.body', '影像/摄影/相机机身', r'相机|微单|单反|(?<![A-Za-z0-9])[AZR]\d{1,2}(?:CR|[CRS](?:M\d|III|II|IV|V|\d)?|M\d|III|II|IV|V)?(?![A-Za-z0-9])'),
    ('camera.lens', '影像/摄影/镜头', r'镜头|\d{2,3}-\d{2,3}\s*(?:mm|F)|定焦|变焦'),
    ('camera.action', '影像/摄像/运动与全景相机', r'GoPro|Insta360|影石|运动相机|全景相机|Osmo\s*Action'),
    ('camera.pocket', '影像/摄像/口袋云台相机', r'Pocket|口袋相机'),
    ('camera.drone', '影像/航拍/无人机', r'无人机|御\s*\d|Mavic|DJI\s*(?:Mini|Air|Avata)'),
    ('camera.gimbal', '影像/配件/稳定器与脚架', r'稳定器|三脚架|云台|曼富图'),
    ('camera.light', '影像/配件/补光与闪光设备', r'补光灯|闪光灯|摄影灯'),
    ('wearable.watch', '穿戴/电子/智能手表', r'Watch|智能手表|电话手表|儿童(?:智能)?手表'),
    ('wearable.band', '穿戴/电子/手环', r'手环|\bBand\s*\d'),
    ('wearable.mechanical', '穿戴/传统/机械与石英表', r'机械表|石英表|卡西欧|劳力士|欧米茄|精工'),
    ('home.cleaning', '家电/清洁/扫地与洗地设备', r'扫地|洗地机|吸尘器'),
    ('home.air', '家电/环境/空气与温控设备', r'净化器|加湿器|除湿机|空调|暖风|电暖'),
    ('home.kitchen', '家电/厨房/厨房电器', r'咖啡机|磨豆机|电饭煲|微波炉|烤箱|冰箱|洗碗机|空气炸锅'),
    ('home.laundry', '家电/洗护/洗衣与烘干设备', r'洗衣机|烘干机|挂烫机|熨斗'),
    ('home.personal', '家电/个护/个护电器', r'剃须刀|吹风机|电动牙刷|冲牙器'),
    ('home.smart', '家居/智能/智能家居', r'智能门锁|智能开关|智能灯|传感器|家庭网关'),
    ('home.furniture', '家居/家具/桌椅与家具', r'工学椅|人体工学|桌子|升降桌|椅子|沙发|床垫'),
    ('life.clothing', '生活/穿戴/服饰', r'衣服|羽绒服|外套|冲锋衣|裤子|T恤'),
    ('life.shoes', '生活/穿戴/鞋靴', r'鞋|球鞋|靴'),
    ('life.bag', '生活/穿戴/箱包', r'背包|双肩包|行李箱|旅行箱|皮包'),
    ('life.cycling', '生活/运动/骑行设备', r'自行车|公路车|山地车|骑行|头盔'),
    ('life.fitness', '生活/运动/健身与户外用品', r'哑铃|健身|帐篷|露营|鱼竿|钓鱼'),
    ('life.car', '生活/交通/车辆与汽车用品', r'汽车|摩托|电动车|三轮车|行车记录仪|轮胎'),
    ('life.toy', '生活/收藏/玩具与模型', r'乐高|手办|模型|玩具'),
    ('life.book', '生活/文化/书籍与乐器', r'书籍|图书|作品集|几本.{0,6}书|吉他|钢琴|电子琴'),
    ('life.tools', '生活/工具/工具与仪器', r'电钻|螺丝刀|万用表|示波器|焊台'),
    ('virtual.ai_membership', '虚拟权益/会员/AI服务会员', r'(?:ChatGPT|Claude|Grok|Gemini|Kimi|Cursor|Codex|GPT).{0,16}(?:会员|订阅|车位|账号|账户|合租|Team|权益)|AI.{0,6}会员'),
    ('virtual.video_membership', '虚拟权益/会员/影视会员', r'Netflix|YouTube\s*(?:Premium|会员)|爱奇艺|优酷|腾讯视频|哔哩哔哩.{0,8}会员|Bilibili.{0,8}会员|网飞|奈飞|Disney\s*\+?|影音会员|视频会员'),
    ('virtual.music_membership', '虚拟权益/会员/音乐会员', r'Spotify|Apple\s*Music|QQ音乐|网易云.{0,8}会员|音乐会员'),
    ('virtual.retail_membership', '虚拟权益/会员/零售会员', r'山姆|开市客|88\s*VIP|京东.{0,3}PLUS|Costco'),
    ('virtual.membership', '虚拟权益/会员/类型未注明', r'会员|订阅|亲友卡|\b(?:gpt|claude|kimi).{0,12}(?:车位|team|code|权益)'),
    ('virtual.giftcard', '虚拟权益/卡券/礼品卡', r'礼品卡|京东卡|狗东卡|E卡'),
    ('virtual.software', '虚拟权益/授权/软件与游戏授权', r'激活码|授权码|兑换码|软件授权|游戏账号'),
    ('virtual.cloud_host', '虚拟权益/云资源/云主机与VPS', r'云服务器|云主机|\bVPS\b|\bVDS\b|azure.{0,12}账号'),
    ('virtual.domain', '虚拟权益/网络资源/域名', r'域名'),
    ('virtual.sim', '虚拟权益/通信/手机卡与流量套餐', r'流量卡|电话卡|手机卡|SIM卡|e-?SIM.{0,8}(?:流量|套餐|激活码)'),
    ('virtual.mobile_credit', '虚拟权益/通信/话费权益', r'话费'),
    ('virtual.proxy', '虚拟权益/网络服务/代理订阅', r'机场.{0,12}(?:订阅|套餐|流量|账号|账户)|代理订阅'),
    ('virtual.pt_invite', '虚拟权益/社区权益/PT邀请', r'\bPT.{0,6}邀|邀.{0,6}\bPT\b|馒头.{0,6}邀|柠檬.{0,6}邀'),
    ('virtual.service', '虚拟权益/服务/服务权益', r'云资源|网络服务|机场|京东\s*(?:家政|保洁)|天鹅到家.{0,6}(?:保洁|家政)'),
]
# Older subject recognizers may distinguish platforms to resolve model names;
# the user-facing taxonomy never turns a brand into a product category.
ALIASES = {key:'phone.smartphone' for key in ('phone.apple','phone.android','phone.unspecified')}
ALIASES.update({key:'tablet.tablet' for key in ('tablet.ipad','tablet.android','tablet.other')})
CATEGORIES = {ALIASES.get(key,key):label for key,label,_ in RULES}
CATEGORIES.update({'phone.smartphone':'手机/智能手机/智能手机','tablet.tablet':'平板/平板电脑/平板电脑'})

GROUPS = {
    'phone':'手机','tablet':'平板与阅读器','computer':'电脑整机','component':'电脑配件',
    'storage':'存储','network':'网络设备','display':'显示设备','input':'电脑外设',
    'office':'办公设备','game':'游戏','audio':'音频','camera':'影像','wearable':'穿戴',
    'accessory':'数码配件','home':'家居与家电','life':'生活用品','virtual':'虚拟权益',
}
TYPE_FAMILIES = {
    'component.memory':('内存',('component.memory',)),
    'storage.ssd':('固态硬盘',('storage.ssd',)),
    'network.router':('路由器',('network.router',)),
    'audio.headphones':('耳机',('audio.tws','audio.inear','audio.overear','audio.headphone')),
    'audio.speakers':('音箱',('audio.speaker','audio.portable')),
    'virtual.memberships':('会员',('virtual.membership','virtual.ai_membership','virtual.video_membership',
                               'virtual.music_membership','virtual.retail_membership')),
}


def canonical(key: str) -> str:
    return ALIASES.get(key,key)


def group_for(key: str) -> str:
    return canonical(key).split('.',1)[0]


def type_for(key: str) -> str:
    key=canonical(key)
    for family,(_,prefixes) in TYPE_FAMILIES.items():
        if any(key==prefix or key.startswith(prefix+'_') for prefix in prefixes):
            return family
    return key


def hierarchy() -> dict:
    result={key:{'label':label,'types':{}} for key,label in GROUPS.items()}
    for key,label in CATEGORIES.items():
        group=group_for(key);kind=type_for(key)
        name=TYPE_FAMILIES[kind][0] if kind in TYPE_FAMILIES else label.split('/')[-1]
        item=result[group]['types'].setdefault(kind,{'label':name,'categories':[]})
        item['categories'].append(key)
    return result
