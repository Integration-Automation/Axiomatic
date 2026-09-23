"""該寫繁體的地方有沒有混進**非台灣標準字形**——既有兩道語言守門都沒管的方向。

本專案原本有兩道，各管一個方向：

    test_docs_sync   zh-CN 段落不得漂回繁體
    test_language    繁體文字不得用大陸**詞彙**（`最佳化` 寫成 `優化`）
    （本檔）         繁體文字不得混進**簡體字**（寫成 `优化`）

第三個方向為什麼會漏掉：`test_language` 比對的是**詞**，而那份詞表兩側都是繁體字，
所以連字都換掉的寫法反而穿得過去——它不在詞表裡。實測抓到三筆真的外洩。

**判定用一張內嵌字表，沒有任何第三方 import，這是刻意的。** 第一版 `import opencc`
當場撞上 `test_bot_helpers.test_every_third_party_import_is_declared_in_requirements`，
而那條守門是對的。三條路都走不通，第四條才對：

* 宣告進 `requirements.txt`——`opencc` 不是 bot 功能的相依，這會讓每個 fresh clone
  都裝一個只有手動工具用得到的套件。`pip-audit` 的處理方式（不宣告，由
  `audit_dependencies.py` 以子行程呼叫）就是這條路的既有判例。
* 開一條豁免——`_top_level_imports` 的 docstring 明講「函式內與 `try:` 內的 import
  一樣要算」，理由是漏宣告的可選相依會讓 fresh clone **安靜地少一組功能**。
* 只當手動工具不做閘門——`.venv` 沒裝 `opencc`，那會變成在正式直譯器上永遠跳過。

內嵌字表把這三個問題一次解掉：兩個直譯器行為一致，所以它可以是真的閘門
（`test_language.test_no_simplified_characters_leak_into_traditional_text`），
而 fresh clone 一毛錢都不必付。字表是**固定資料**，簡繁對應不會漂。

**字表怎麼重新產生**（需要 `py -3 -m pip install opencc`；產生器刻意不放進樹裡，
一次性腳本照本專案慣例留在 repo 外）：拿 `OpenCC("s2tw")` 掃過 U+4E00–U+9FFF 與
U+3400–U+4DBF，收集 `convert(ch) != ch` 的字元。

**`s2tw` 不是 `s2t`，這是整件事最貴的一課。** `s2t` 轉的是「正統繁體」，所以
`台→臺`、`吃→喫`、`群→羣`、`峰→峯`、`灶→竈`、`床→牀`、`秘→祕`、`污→汙` 這些
台灣本來就這樣寫的異體字也照轉——實測 2,521 筆命中裡 **2,405 筆**是這個原因，光
`台` 一個字就 1,122 筆。

**這支實際檢查的是「台灣標準字形」，比「簡體」廣，而那是好事。** `喫`／`羣`／`峯`
這些不是簡體、是正統繁體的異體寫法，`s2tw` 一樣會正規化掉它們。在這個專案裡那正是
語言規則要的東西，所以不排除。

命令列用法（結束碼 0＝乾淨，1＝有需要人看的命中，2＝自我檢查失敗）：

    py -3 axiomatic/audit_simplified_chars.py [--show-known]
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PKG_ROOT.parent

# 佇列與提示詞**資料檔**不掃——那些是使用者的內容，不是本專案的文字。
# 這份名單與 `test_language._DATA_MARKDOWN` 是同一個判準，兩邊要一起改。
_DATA_MARKDOWN = {
    "auth.md", "discord_bot_token.md",
    "character1.md", "character2.md",
    "default_prompt.md", "prompt.md", "undesired.md",
    "todo_character1.md", "todo_character2.md", "todo_character_2_default.md",
    "todo_prompt.md", "todo_undesired.md",
}

# 台灣中文本來就在用、但 `s2tw` 仍會轉的字。每一個都要寫理由——與 `test_language`
# 的詞表刻意不收「通過／文件／程序」是同一個原則：會亂叫的守門會被關掉。
AMBIGUOUS = {
    "台": "台灣／平台／站台",
    "准": "准許／批准（只有「標準」那個義項該寫準）",
    "游": "下游／上游／游泳（只有「遊戲」那個義項該寫遊）",
    "干": "干擾／干涉／若干",
    "划": "划算／划船",
    "占": "占卜／獨占",
    "里": "公里／鄉里／十萬八千里",
    "余": "余為姓氏（但「冗余」確實該寫冗餘，這個義項只能人看）",
    "污": "污染／污點——台灣標準就寫污，汙是異體",
}

# 刻意保留的簡體，逐筆寫理由。比對方式是「檔名結尾 ＋ 該行含這個子字串」，不用行號
# ——行號一天就漂掉了。過期的條目會被報出來：一個永遠對不上的豁免會安靜失效，
# 而守門看起來照常在跑（與 `_OWNER_ONLY_SLASH` 同一個形狀）。
# **整份檔案刻意不是台灣繁體**的那幾個，檔名 → 理由。逐行豁免在這裡沒有意義：
# 一份簡體或日文的文件每一行都會命中，而一份寫滿理由的逐行清單只會讓人以為這件事
# 沒人管。列進來的檔案整份跳過，但**兩個方向都對帳**（見 `stale_file_rules`）：
# 檔案要存在，而且要真的含有非台灣標準字形——一個永遠對不上的豁免會安靜失效。
DELIBERATE_FILES: dict[str, str] = {
    "README.zh-CN.md": "四語 README 的簡體版，整份刻意是簡體（見 test_readme_parity）",
    "README.ja.md": "四語 README 的日文版。日文漢字有大量新字體，與簡體字表重疊，"
                    "而它本來就不是中文——語言規則管的是「本專案寫的中文」",
}

DELIBERATE: tuple[tuple[str, str, str], ...] = (
    ("discord_bot.py", '"zh-hans"',
     "語言代碼對照表，鍵本身就是使用者會輸入的簡體"),
    ("discord_bot.py", '"simplified"',
     "同一張對照表的下一行"),
    ("discord_bot.py", "_DOROSSI_CONTINUE_VERBS",
     "使用者會打的指令動詞別名"),
    ("test_docs_sync.py", "@bot <",
     "zh-CN help 語料的預期值"),
    ("test_docs_sync.py", "# zh-CN 引用樣式",
     "四語 README 的簡體版引用樣式，行尾標記讓整組只要一筆豁免"),
    ("README.md", "简体中文",
     "語言切換器裡那個語言自己的名稱，不寫簡體就連不回去"),
    ("README.zh-TW.md", "简体中文", "同上"),
    ("test_readme_parity.py", "简体中文",
     "語言切換器的正本清單（檔名 → 那個語言自己的名稱）"),
    ("test_docs_sync.py", "改法：把那幾行改寫成",
     "這是 zh-CN 守門自己的失敗訊息，本來就在教人寫簡體"),
    ("test_docs_sync.py", "不要**改成繁體去",
     "同一段失敗訊息的下一行"),
)

# 自我檢查用。兩側都要：只驗「該抓的有抓到」的話，一張**全部都收**的字表也會全過。
_MUST_DETECT = "这们时说对实现问网优队传输数据设备变电员国语认识证试话该请读谁调谢"
_MUST_ALLOW = "的是那只走窗人天日月水火山中大小上下不了在有我你他台准游干划占里余污"

_NOT_TW_STANDARD_SRC = (
    "万与丑专业丛东丝丢两严丧个丰临为丽举么义乌乐乔习乡书买乱争于亏云亘亚产亩亲亵亸亿仅仆从仑仓仪们价"
    "众优伙会伛伞伟传伡伣伤伥伦伧伪伫体余佣佥侠侣侥侦侧侨侩侪侬侭俣俦俨俩俪俫俭债倾偬偻偾偿傤傥傧储傩"
    "僞儿兑兖党兰关兴兹养兽冁内冈册写军农冯冲决况冻净凄准凉减凑凛几凤凫凭凯凶击凿刍划刘则刚创删别刬刭"
    "刹刽刾刿剀剂剐剑剥剧劝办务劢动励劲劳势勋勚匀匦匮区医华协单卖占卢卤卧卫却卺厂厅历厉压厌厍厐厕厘厢"
    "厣厦厨厩厮县叁参叆叇双发变叙叠台叶号叹叽吁后吓吕吗吨听启吴呐呒呓呕呖呗员呙呛呜咏咙咛咝咤咨咸响哑"
    "哒哓哔哕哗哙哜哝哟唛唝唠唡唢唤啓啧啬啭啮啯啰啴啸喫喷喽喾嗫嗳嘘嘤嘱噜嚣团园囱围囵国图圆圣圹场坏块"
    "坚坛坜坝坞坟坠垄垅垆垒垦垩垫垭垯垱垲垴埘埙埚堑堕塆墙壮声壳壶壸处备复够头夸夹夺奁奂奋奖奥妆妇妈妩"
    "妪妫姗姹娄娅娆娇娈娱娲娴婳婴婵婶媪媭嫒嫔嫱嫺嬀嬷孙学孪宁宝实宠审宪宫宽宾寝对寻导寿将尔尘尝尧尴尸"
    "尽层屃屉届属屡屦屿岁岂岖岗岘岚岛岩岭岳岽岿峃峄峡峣峤峥峦峯崂崃崄崭嵘嵚嵝巅巩巯币帅师帏帐帘帜带帧"
    "帮帱帻帼幂干并幺广庄庆庐庑库应庙庞废庼廪开异弃弑张弥弪弯弹强归当录彟彦彨彻征径徕忆忏忧忾怀态怂怃"
    "怄怅怆怜总怼怿恋恒恳恶恸恹恺恻恼恽悦悫悬悭悮悯惊惧惨惩惫惬惭惮惯愠愤愦愿慑慭懑懒懔戆戋戏戗战戬戯"
    "户扑托执扩扪扫扬扰抚抛抟抠抡抢护报担拟拢拣拥拦拧拨择挂挚挛挜挝挞挟挠挡挢挣挤挥挦捝捞损捡换捣据掳"
    "掴掷掸掺掼揽揾揿搀搁搂搄搅携摄摅摆摇摈摊撄撑撵撷撸撺擜擞擡攒敌敚敛敩数斋斓斗斩断无旧时旷旸昙昵昼"
    "昽显晋晒晓晔晕晖暂暅暧术朴机杀杂权杠条来杨杩杰极构枞枢枣枥枧枨枪枫枭柜柠柽栀栅标栈栉栊栋栌栎栏树"
    "栖栗样栾桠桡桢档桤桥桦桧桨桩桪梦梼梾梿检棁棂棱椁椝椟椠椢椤椫椭椮楼榄榅榇榈榉榝槚槛槟槠横樯樱橥橱"
    "橹橼檐檩欢欤欧歼殁殇残殒殓殚殡殴毁毂毕毙毡毵毶氇气氢氩氲汇汉污汤汹沄沟没沣沤沥沦沧沨沩沪泄泞泪泶"
    "泷泸泺泻泼泽泾洁洒洼浃浅浆浇浈浉浊测浍济浏浐浑浒浓浔浕涂涌涚涛涝涞涟涠涡涢涣涤润涧涨涩淀渊渌渍渎"
    "渐渑渔渖渗温游湾湿溁溃溅溆溇滗滚滞滟滠满滢滤滥滦滨滩滪潆潇潋潍潙潜潨潴澛澜濑濒灏灭灯灵灾灿炀炉炖"
    "炜炝点炼炽烁烂烃烛烟烦烧烨烩烫烬热焕焖焘煴熏爱爲爷牀牍牦牵牺犊状犷犸犹狈狝狞独狭狮狯狰狱狲猃猎猕"
    "猡猪猫猬献獭玑玙玚玛玮环现玱玺珐珑珰珲琎琏琐琼瑶瑷瑸璎瓒瓮瓯电画畅畴疖疗疟疠疡疬疭疮疯疱疴痈痉痒"
    "痖痨痪痫痹瘅瘆瘗瘘瘪瘫瘾瘿癞癡癣癫皁皑皱皲盏盐监盖盗盘眍眦眬着睁睐睑睾瞆瞒瞩矫矶矾矿砀码砖砗砚砜"
    "砺砻砾础硁硕硖硗硙硚确硵硷碍碛碜碱礼祃祎祕祢祯祷祸禀禄禅离秃秆种积称秽秾稆税稣稳穑穞穷窃窍窎窑窜"
    "窝窥窦窭竈竖竞笃笋笔笕笺笼笾筑筚筛筜筝筹筼签筿简箓箦箧箨箩箪箫篑篓篮篯篱簖籁籴类籼粜粝粤粪粮糁糇"
    "糉糍紧絷緼縆繮纔纟纠纡红纣纤纥约级纨纩纪纫纬纭纮纯纰纱纲纳纴纵纶纷纸纹纺纻纼纽纾线绀绁绂练组绅细"
    "织终绉绊绋绌绍绎经绐绑绒结绔绕绖绗绘给绚绛络绝绞统绠绡绢绣绤绥绦继绨绩绪绫绬续绮绯绰绱绲绳维绵绶"
    "绷绸绹绺绻综绽绾绿缀缁缂缃缄缅缆缇缈缉缊缋缌缍缎缏缐缑缒缓缔缕编缗缘缙缚缛缜缝缞缟缠缡缢缣缤缥缦"
    "缧缨缩缪缫缬缭缮缯缰缱缲缳缴缵罂网罗罚罢罴羁羟羡羣翘翙翚耢耧耸耻聂聋职聍联聩聪肃肠肤肮肴肾肿胀胁"
    "胆胜胧胨胪胫胶脉脍脏脐脑脓脔脚脣脱脶脸腊腌腘腭腻腼腽腾膑膻臜舆舣舰舱舻艰艳艺节芈芗芜芦苁苇苈苋苌"
    "苍苎苏苧苹范茎茏茑茔茕茧荆荐荙荚荛荜荝荞荟荠荡荣荤荥荦荧荨荩荪荫荬荭荮药莅莱莲莳莴莶获莸莹莺莼萚"
    "萝萤营萦萧萨葱蒀蒇蒉蒋蒌蒏蓝蓟蓠蓣蓥蓦蔂蔘蔷蔹蔺蔼蔿蕰蕲蕴薮藓藴蘖虏虑虚虫虬虮虱虽虾虿蚀蚁蚂蚃蚕"
    "蚝蚬蛊蛎蛏蛮蛰蛱蛲蛳蛴蜕蜗蜡蝇蝈蝉蝎蝼蝾螀螨蟏衅衆衔补衬衮袄袅袆袜袭袯装裆裈裏裢裣裤裥褛褴襕覈见"
    "观觃规觅视觇览觉觊觋觌觍觎觏觐觑觞触觯訚詟誉誊讠计订讣认讥讦讧讨让讪讫讬训议讯记讱讲讳讴讵讶讷许"
    "讹论讻讼讽设访诀证诂诃评诅识诇诈诉诊诋诌词诎诏诐译诒诓诔试诖诗诘诙诚诛诜话诞诟诠诡询诣诤该详诧诨"
    "诩诪诫诬语诮误诰诱诲诳说诵诶请诸诹诺读诼诽课诿谀谁谂调谄谅谆谇谈谉谊谋谌谍谎谏谐谑谒谓谔谕谖谗谘"
    "谙谚谛谜谝谞谟谠谡谢谣谤谥谦谧谨谩谪谫谬谭谮谯谰谱谲谳谴谵谶豮贝贞负贠贡财责贤败账货质贩贪贫贬购"
    "贮贯贰贱贲贳贴贵贶贷贸费贺贻贼贽贾贿赀赁赂赃资赅赆赇赈赉赊赋赌赍赎赏赐赑赒赓赔赕赖赗赘赙赚赛赜赝"
    "赞赟赠赡赢赣赪赵赶趋趱趸跃跄跖跞践跶跷跸跹跻踊踌踪踬踯蹑蹒蹰蹿躏躜躯輼车轧轨轩轪轫转轭轮软轰轱轲"
    "轳轴轵轶轷轸轹轺轻轼载轾轿辀辁辂较辄辅辆辇辈辉辊辋辌辍辎辏辐辑辒输辔辕辖辗辘辙辚辞辟辩辫边辽达迁"
    "过迈运还这进远违连迟迩迳迹适选逊递逦逻遗遥邓邝邬邮邹邺邻郁郏郐郑郓郦郧郸酂酝酦酱酽酾酿醖采释里鉢"
    "鉴銮錾鍼钅钆钇针钉钊钋钌钍钎钏钐钑钒钓钔钕钖钗钘钙钚钛钜钝钞钟钠钡钢钣钤钥钦钧钨钩钪钫钬钭钮钯钰"
    "钱钲钳钴钵钶钷钸钹钺钻钼钽钾钿铀铁铂铃铄铅铆铇铈铉铊铋铌铍铎铏铐铑铒铓铔铕铖铗铘铙铚铛铜铝铞铟铠"
    "铡铢铣铤铥铦铧铨铩铪铫铬铭铮铯铰铱铲铳铴铵银铷铸铹铺铻铼铽链铿销锁锂锃锄锅锆锇锈锉锊锋锌锍锎锏锐"
    "锑锒锓锔锕锖锗锘错锚锛锜锝锞锟锠锡锢锣锤锥锦锧锨锩锪锫锬锭键锯锰锱锲锳锴锵锶锷锸锹锺锻锼锽锾锿镀"
    "镁镂镃镄镅镆镇镈镉镊镋镌镍镎镏镐镑镒镓镔镕镖镗镘镙镚镛镜镝镞镟镠镡镢镣镤镥镦镧镨镩镪镫镬镭镮镯镰"
    "镱镲镳镴镵镶长门闩闪闫闬闭问闯闰闱闲闳间闵闶闷闸闹闺闻闼闽闾闿阀阁阂阃阄阅阆阇阈阉阊阋阌阍阎阏阐"
    "阑阒阓阔阕阖阗阘阙阚阛队阳阴阵阶际陆陇陈陉陕陦陧陨险随隐隶隽难雇雏雠雳雾霁霉霡霭靓靔静靥鞑鞒鞯鞲"
    "韦韧韨韩韪韫韬韵页顶顷顸项顺须顼顽顾顿颀颁颂颃预颅领颇颈颉颊颋颌颍颎颏颐频颒颓颔颕颖颗题颙颚颛颜"
    "额颞颟颠颡颢颣颤颥颦颧风飏飐飑飒飓飔飕飖飗飘飙飚飞飨餍饣饤饥饦饧饨饩饪饫饬饭饮饯饰饱饲饳饴饵饶饷"
    "饸饹饺饻饼饽饾饿馀馁馂馃馄馅馆馇馈馉馊馋馌馍馎馏馐馑馒馓馔馕马驭驮驯驰驱驲驳驴驵驶驷驸驹驺驻驼驽"
    "驾驿骀骁骂骃骄骅骆骇骈骉骊骋验骍骎骏骐骑骒骓骔骕骖骗骘骙骚骛骜骝骞骟骠骡骢骣骤骥骦骧髅髋髌鬓鬶魇"
    "魉鮎鱼鱽鱾鱿鲀鲁鲂鲃鲄鲅鲆鲇鲈鲉鲊鲋鲌鲍鲎鲏鲐鲑鲒鲓鲔鲕鲖鲗鲘鲙鲚鲛鲜鲝鲞鲟鲠鲡鲢鲣鲤鲥鲦鲧鲨鲩"
    "鲪鲫鲬鲭鲮鲯鲰鲱鲲鲳鲴鲵鲶鲷鲸鲹鲺鲻鲼鲽鲾鲿鳀鳁鳂鳃鳄鳅鳆鳇鳈鳉鳊鳋鳌鳍鳎鳏鳐鳑鳒鳓鳔鳕鳖鳗鳘鳙"
    "鳚鳛鳜鳝鳞鳟鳠鳡鳢鳣鳤鸟鸠鸡鸢鸣鸤鸥鸦鸧鸨鸩鸪鸫鸬鸭鸮鸯鸰鸱鸲鸳鸴鸵鸶鸷鸸鸹鸺鸻鸼鸽鸾鸿鹀鹁鹂鹃"
    "鹄鹅鹆鹇鹈鹉鹊鹋鹌鹍鹎鹏鹐鹑鹒鹓鹔鹕鹖鹗鹘鹙鹚鹛鹜鹝鹞鹟鹠鹡鹢鹣鹤鹥鹦鹧鹨鹩鹪鹫鹬鹭鹮鹯鹰鹱鹲鹳"
    "鹴鹾麦麪麸麹麺麽黄黉黡黩黪黾鼋鼌鼍鼹齐齑齶齿龀龁龂龃龄龅龆龇龈龉龊龋龌龙龚龛龟鿎鿏鿒鿔㐷㐹㐽㑇㑈"
    "㑔㑩㓆㓥㓰㔉㖊㖞㘎㚯㛀㛟㛠㛣㛤㛿㟆㟜㟥㡎㤘㤽㥪㧏㧐㧑㧟㧰㨫㭎㭏㭣㭤㭴㱩㱮㲿㳔㳕㳠㳡㳢㳽㴋㶉㶶㶽㺍"
    "㻅㻏㻘䀥䁖䂵䃅䅉䅟䅪䇲䉤䌶䌷䌸䌹䌺䌻䌼䌽䌾䌿䍀䍁䍠䎬䏝䑽䓓䓕䓖䓨䗖䘛䘞䙊䙌䙓䜣䜤䜥䜧䜩䝙䞌䞍䞎䞐"
    "䟢䢀䢁䢂䥺䥽䥾䥿䦀䦁䦂䦃䦅䦆䦶䦷䩄䭪䯃䯄䯅䲝䲞䲟䲠䲡䲢䲣䴓䴔䴕䴖䴗䴘䴙䶮"
)

NOT_TW_STANDARD = frozenset(_NOT_TW_STANDARD_SRC) - set(AMBIGUOUS)


def self_check() -> list[str]:
    """回傳自我檢查的問題清單；空的代表字表可信。"""
    problems = []
    missed = [ch for ch in _MUST_DETECT if ch not in NOT_TW_STANDARD]
    if missed:
        problems.append(f"這些一定是簡體的字沒被收進字表：{''.join(missed)}")
    wrong = [ch for ch in _MUST_ALLOW if ch in NOT_TW_STANDARD]
    if wrong:
        problems.append(f"這些台灣正當用字被收進字表了：{''.join(wrong)}")
    if len(NOT_TW_STANDARD) < 2000:
        problems.append(f"字表只有 {len(NOT_TW_STANDARD)} 個字，像是被截斷了")
    return problems


def sources() -> list[Path]:
    """要掃的檔案。

    與 `test_language._sources()` 同一個範圍。測試住在 repo 根目錄的 `test/`，
    所以那個目錄要另外列（它們不在套件的 glob 裡）。
    """
    found: list[Path] = sorted(PKG_ROOT.glob("*.py"))
    found += sorted((REPO_ROOT / "test").glob("*.py"))
    found += sorted(REPO_ROOT.glob("*.py"))
    found += sorted((REPO_ROOT / "docs").glob("*.py"))
    found += [p for p in sorted(REPO_ROOT.glob("*.md"))
              if p.name not in _DATA_MARKDOWN]
    for folder in ("docs", "commands", "bot_prompts"):
        found += sorted((REPO_ROOT / folder).glob("*.md"))
    # 本檔自己必然帶著它要找的東西（整張字表）。這是刻意的自我豁免。
    return [p for p in found if p.resolve() != Path(__file__).resolve()]


def zh_cn_line_ranges(path: Path) -> list[tuple[int, int]]:
    """`.py` 裡 `*_ZH_CN` 常數佔的行號區間（刻意維持簡體，見 `CLAUDE.md`）。"""
    if path.suffix != ".py":
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    ranges: list[tuple[int, int]] = []
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id.endswith("_ZH_CN"):
                ranges.append((node.lineno, node.end_lineno or node.lineno))
    return ranges


def outside_code_spans(line: str) -> str:
    """把反引號 code span 的內容挖掉——那是**引用**這個字，不是在用它。

    與 `test_language._outside_code_spans` 是**同一條規則**，刻意同名同語意：
    規則之記錄必須舉得出反例才說得清楚，
    而舉例的正確寫法在這個 repo 裡本來就是包進反引號。兩邊要改一起改。

    這是**放寬**步驟，所以殺得掉它的只有 must-allow 樣本（反引號裡的簡體字不能報），
    只餵「該報的」永遠驗不出它被刪掉。
    """
    return "".join(part for index, part in enumerate(line.split("`"))
                   if index % 2 == 0)


def matching_rules(path: Path, line: str) -> list[tuple[str, str, str]]:
    """這一行對上了哪幾條列管規則（可能不只一條）。

    「對上了沒有」與「是哪幾條對上的」**必須是同一個判準**，所以只有這裡寫得出
    `path.name.endswith(suffix) and needle in line`。2026-09-20 以前是兩份：一支只回
    **第一條**對上的規則的理由，`scan()` 再自己寫一次同樣的比對、多接一個
    「理由文字相等」的條件去跟那個回傳值對帳——那是拿理由當外鍵，於是一行同時對上
    兩條規則時，只有第一條會被記成「用過」，第二條明明正在做事卻被 `stale_rules()`
    報成過期。實測重現過：兩條規則、同一行，`stale` 回了第二條。那個方向是**誤報**，
    而本專案對誤報的判語是「a guard that cries wolf is a guard someone switches off」。
    """
    return [rule for rule in DELIBERATE
            if path.name.endswith(rule[0]) and rule[1] in line]


def scan():
    """回傳 `(需要人看, 已知刻意, 用到的豁免規則, (檔數, 豁免行數))`。"""
    review: list[tuple[str, int, str, str]] = []
    known: list[tuple[str, int, str, str]] = []
    used: set[tuple[str, str]] = set()
    files = sources()
    exempted = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            print(f"※ 讀不了 {path.name}：{error!r}")
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if path.name in DELIBERATE_FILES:
            # 整份跳過，但**要算進 `exempted`**：那個計數是呼叫端的正面對照組，
            # 不算的話「整份豁免」與「這個檔案根本沒被掃到」長得一模一樣。
            exempted += sum(
                1 for line in text.splitlines()
                if any(ch in NOT_TW_STANDARD for ch in outside_code_spans(line)))
            continue
        skip = zh_cn_line_ranges(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            found = sorted({ch for ch in outside_code_spans(line)
                            if ch in NOT_TW_STANDARD})
            if not found:
                continue
            if any(lo <= lineno <= hi for lo, hi in skip):
                exempted += 1
                continue
            rules = matching_rules(path, line)
            row = (rel, lineno, "".join(found), line.strip()[:95])
            if not rules:
                review.append(row)
            else:
                known.append(row)
                used.update((suffix, needle) for suffix, needle, _ in rules)
    return review, known, used, (len(files), exempted)


def stale_rules(used) -> list[tuple[str, str]]:
    """一行都沒對上的豁免規則。"""
    return [(suffix, needle) for suffix, needle, _why in DELIBERATE
            if (suffix, needle) not in used]


def stale_file_rules() -> list[str]:
    """`DELIBERATE_FILES` 裡已經沒有意義的條目。

    兩種都算過期：檔案在掃描範圍裡找不到（改名或刪掉了），以及檔案還在、卻一個
    非台灣標準字形都沒有（那它根本不需要豁免，而留著會讓下一個真的該被看的檔案
    被同一筆默默放行）。
    """
    by_name = {path.name: path for path in sources()}
    stale = []
    for name, reason in DELIBERATE_FILES.items():
        path = by_name.get(name)
        if path is None:
            stale.append(f"{name}（不在掃描範圍裡）")
            continue
        if not reason.strip():
            stale.append(f"{name}（沒寫理由）")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            stale.append(f"{name}（讀不了）")
            continue
        if not any(ch in NOT_TW_STANDARD
                   for line in text.splitlines()
                   for ch in outside_code_spans(line)):
            stale.append(f"{name}（整份都是台灣標準字形，不需要豁免）")
    return stale


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="找出該寫繁體的地方混進來的非台灣標準字形。")
    parser.add_argument("--show-known", action="store_true",
                        help="連「已知刻意」那些也印出來")
    opts = parser.parse_args(argv or [])

    problems = self_check()
    if problems:
        print("※ 查不成：字表自我檢查失敗。")
        for item in problems:
            print(f"    {item}")
        return 2

    review, known, used, (n_files, exempted) = scan()
    stale = stale_rules(used)

    print(f"字表 {len(NOT_TW_STANDARD)} 個字（排除 {len(AMBIGUOUS)} 個歧義字），"
          f"自我檢查通過")
    print(f"掃了 {n_files} 個檔；`*_ZH_CN` 區段豁免掉 {exempted} 行"
          + ("  ※ 這裡是 0，豁免抓法可能壞了" if not exempted else ""))
    print(f"列管的刻意簡體 {len(known)} 行（{len(DELIBERATE)} 筆規則，"
          f"用到 {len(DELIBERATE) - len(stale)} 筆）")

    if stale:
        print("\n※ 這些豁免規則一行都沒對上，可能已經過期——"
              "過期的豁免會安靜失效，而守門看起來照常在跑：")
        for suffix, needle in stale:
            print(f"    {suffix}  ：  {needle}")

    if opts.show_known:
        print(f"\n=== 已知刻意（{len(known)}）===")
        for rel, lineno, chars, snippet in known:
            print(f"  {rel}:{lineno}  [{chars}]  {snippet}")

    print(f"\n=== 需要人看：{len(review)} ===")
    for rel, lineno, chars, snippet in review:
        print(f"  {rel}:{lineno}")
        print(f"      [{chars}]  {snippet}")
    if not review:
        print("  （沒有。）")
    else:
        print("\n判定方式：確認那個字在**台灣中文**裡不是正當寫法，是的話就改掉；"
              "只是在**舉例**的話包進反引號；真的是刻意的（zh-CN 語料、使用者會打的"
              "別名、繁簡對照表），加進 `DELIBERATE` 並寫下理由。")
    return 1 if review else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
