from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf

COURSES = [
    {
        "name": "course_1_architecture.pdf",
        "pages": 104,
        "chapters": [
            ("第1章 计算机网络体系结构", 1),
            ("第2章 物理层", 27),
            ("第3章 数据链路层", 53),
            ("第4章 网络层", 79),
        ],
        "topics": [
            [
                "OSI 七层模型从物理层到应用层依次划分职责",
                "TCP/IP 四层模型把会话、表示、应用合并为应用层",
                "封装是逐层添加首部，解封装是逐层剥离首部",
                "协议数据单元在不同层分别称为比特、帧、分组和报文段",
                "对等层通信依靠协议，相邻层通信依靠服务接口",
            ],
            [
                "奈奎斯特定理给出理想低通信道的最大码元速率",
                "香农公式用带宽和信噪比计算信道容量",
                "基带编码把数字信号直接映射为电平，频带调制把信号搬到载波",
                "双绞线适合短距离局域网，光纤适合长距离高带宽传输",
                "物理层只负责比特传输，不负责差错恢复和路由选择",
            ],
            [
                "帧定界用标志字节或位填充区分相邻帧",
                "循环冗余校验 CRC 用生成多项式检测传输差错",
                "停止等待协议每发一帧就等待确认，信道利用率低",
                "滑动窗口允许连续发送多帧，用窗口大小控制未确认数据量",
                "CSMA/CD 在共享以太网中先听后发、边发边听、冲突后退避",
            ],
            [
                "IPv4 地址由网络号和主机号组成，子网掩码划分两者边界",
                "CIDR 用前缀长度表示网络，支持无类别地址聚合",
                "ARP 在同一网段内把 IP 地址解析为 MAC 地址",
                "ICMP 用于传递差错报告和网络探测信息",
                "路由表查找采用最长前缀匹配，前缀越长越优先",
            ],
        ],
    },
    {
        "name": "course_2_transport.pdf",
        "pages": 104,
        "chapters": [
            ("第1章 传输层概述", 1),
            ("第2章 TCP 可靠传输", 22),
            ("第3章 TCP 拥塞控制", 44),
            ("第4章 TCP 快速重传与可靠传输", 66),
        ],
        "topics": [
            [
                "传输层用端口号复用和分用上层应用的数据",
                "UDP 提供无连接、不可靠但开销小的数据报服务",
                "TCP 提供面向连接、可靠、字节流式传输",
                "端口号范围分为熟知端口、注册端口和临时端口",
                "套接字由 IP 地址和端口号共同标识",
            ],
            [
                "三次握手用 SYN、SYN-ACK、ACK 同步初始序号",
                "序号标识字节流位置，确认号表示期望收到的下一个字节",
                "超时重传在重传计时器到期后重发未确认报文段",
                "累积确认允许一次确认之前连续收到的多个报文段",
                "流量控制用接收窗口通告接收端可用缓冲区大小",
            ],
            [
                "慢启动从拥塞窗口一个报文段开始并指数增长",
                "拥塞避免阶段每个往返时间线性增加拥塞窗口",
                "超时发生时把慢启动阈值设为当前窗口的一半并重新慢启动",
                "快恢复在收到重复确认后避免窗口直接降为一个报文段",
                "AIMD 的加性增乘性减使发送速率围绕网络容量波动",
            ],
            [
                "重传计时器到期后发送端重发最旧的未确认报文段",
                "快恢复阶段拥塞窗口从慢启动阈值开始增长",
                "选择确认 SACK 用数据块描述非连续接收范围",
                "窗口缩放选项扩大接收窗口通告范围",
                "坚持计时器用于探测零窗口之后的窗口更新",
            ],
        ],
    },
    {
        "name": "course_3_routing.pdf",
        "pages": 104,
        "chapters": [
            ("第1章 路由基础", 1),
            ("第2章 静态路由", 27),
            ("第3章 动态路由", 53),
            ("第4章 路由聚合", 79),
        ],
        "topics": [
            [
                "路由表记录目的网络、下一跳、出接口和度量值",
                "直连路由由接口地址自动生成，优先级通常最高",
                "静态路由由管理员手工配置，不随拓扑变化自动调整",
                "默认路由匹配所有目的地址，常用于出口方向",
                "转发时先查路由表，再按最长前缀选择条目",
            ],
            [
                "静态路由的下一跳可以是相邻路由器接口地址",
                "管理距离用于比较不同路由来源的可信度",
                "浮动静态路由通过提高管理距离作为备份路径",
                "缺省路由减少路由表规模但可能形成环路",
                "静态路由不会自动感知链路故障，需要配合检测机制",
            ],
            [
                "RIP 是距离向量协议，用跳数作为度量且最大跳数为 15",
                "OSPF 是链路状态协议，用 LSA 描述拓扑并运行 SPF 计算",
                "OSPF 邻居建立经历 Down、Init、2-Way、ExStart、Exchange、Loading、Full",
                "BGP 是路径向量协议，在自治系统之间交换可达性信息",
                "水平分割和毒性逆转用于抑制距离向量协议环路",
            ],
            [
                "路由聚合把连续子网合并为更短前缀，减少路由表条目",
                "CIDR 汇总要求被聚合前缀具有相同的高位比特",
                "最长前缀匹配保证聚合后仍能选到更具体的路由",
                "聚合不当可能把流量导向错误方向或形成黑洞",
                "路由汇总的边界应与地址规划中的区域边界一致",
            ],
        ],
    },
    {
        "name": "course_4_security.pdf",
        "pages": 104,
        "chapters": [
            ("第1章 网络安全基础", 1),
            ("第2章 加密与认证", 27),
            ("第3章 防火墙", 53),
            ("第4章 入侵检测", 79),
        ],
        "topics": [
            [
                "CIA 三要素包括机密性、完整性和可用性",
                "威胁模型描述攻击者能力、攻击面和可能造成的损失",
                "安全策略把业务要求转化为可执行的访问控制规则",
                "最小权限原则只授予完成任务所需的最小权限",
                "纵深防御用多层控制降低单点失效风险",
            ],
            [
                "对称加密用同一密钥加解密，速度快但密钥分发困难",
                "非对称加密用公钥加密、私钥解密，适合密钥交换和签名",
                "数字签名用私钥生成、公钥验证，保证完整性和不可否认",
                "证书把公钥与身份绑定，由证书颁发机构 CA 签发",
                "哈希函数把任意长度输入映射为固定长度摘要",
            ],
            [
                "包过滤防火墙按源地址、目的地址、端口和协议放行或拒绝",
                "状态检测防火墙维护连接状态表，只放行属于已建立连接的回包",
                "NAT 把内部私有地址转换为公网地址，隐藏内部结构",
                "DMZ 放置对外服务，限制外部直接访问内网",
                "默认拒绝策略要求显式放行必要流量",
            ],
            [
                "IDS 检测并告警，IPS 在检测后主动阻断",
                "误报是把正常流量判为攻击，漏报是未发现真实攻击",
                "特征检测匹配已知攻击模式，对未知攻击效果有限",
                "异常检测建立正常行为基线，偏离基线时告警",
                "告警分级和响应流程决定检测系统能否落地",
            ],
        ],
    },
    {
        "name": "course_5_bgp.pdf",
        "pages": 101,
        "chapters": [
            ("第1章 自治系统", 1),
            ("第2章 BGP 基础", 26),
            ("第3章 BGP 路径属性", 51),
            ("第4章 BGP 路由反射与选路", 82),
        ],
        "topics": [
            [
                "自治系统 AS 是一组由单一机构管理、使用统一路由策略的网络",
                "IGP 在自治系统内部计算路由，EGP 在自治系统之间交换路由",
                "eBGP 连接不同 AS 的边界路由器，iBGP 连接同一 AS 内路由器",
                "AS 号用于标识自治系统，分为 2 字节和 4 字节格式",
                "BGP 选择路径而非最短路径，重视策略与可达性",
            ],
            [
                "BGP 邻居通过 OPEN 报文协商版本、AS 号和保持时间",
                "KEEPALIVE 报文周期发送以维持邻居关系",
                "UPDATE 报文携带可达前缀、路径属性和撤销信息",
                "BGP 使用 TCP 179 端口建立可靠会话",
                "邻居状态包括 Idle、Connect、Active、OpenSent、OpenConfirm、Established",
            ],
            [
                "AS_PATH 记录前缀经过的自治系统序列，可用于防环和选路",
                "NEXT_HOP 指定到达目的前缀的下一跳地址",
                "LOCAL_PREF 只在同一 AS 内传递，数值越大越优先",
                "MED 用于向相邻 AS 提示入口选择，通常越小越优先",
                "Community 属性用于给路由打标签并配合策略过滤",
            ],
            [
                "BGP 选路先比较权重和 LOCAL_PREF，再比较 AS_PATH 长度",
                "MED 只在相邻自治系统之间比较，缺省值处理会影响结果",
                "前缀列表按前缀和掩码长度过滤路由",
                "路由策略用 route-map 修改属性并决定放行",
                "BGP 收敛速度受邻居数量与策略复杂度影响",
            ],
        ],
    },
]

UNIQUE_POINTS = {
    "course_2_transport.pdf": (
        [65, 66, 67, 68],
        "TCP 快速重传的独有触发条件",
        "接收端连续收到三个重复 ACK 时，发送端不等待重传计时器到期，立即重传丢失的报文段。"
        "三个重复 ACK 必须针对同一报文段，且接收窗口不得为零；该条件只在本资料第 66–67 页说明。",
    ),
    "course_5_bgp.pdf": (
        [82, 83],
        "BGP 路由反射器的簇 ID 规则",
        "路由反射器在簇内反射路由时，不修改 AS_PATH，但必须保留 ORIGINATOR_ID 与 CLUSTER_LIST；"
        "簇 ID 用于防止同一簇内的路由环路，该规则只在本资料第 82–83 页说明。",
    ),
}


def _chapter_of(chapters: list[tuple[str, int]], page_no: int) -> int:
    current = 0
    for index, (_, start) in enumerate(chapters):
        if page_no >= start:
            current = index
    return current


def _add_page(doc, course: dict, page_no: int) -> None:
    chapters = course["chapters"]
    index = _chapter_of(chapters, page_no)
    chapter, _ = chapters[index]
    topics = course["topics"][index]
    page = doc.new_page(width=595, height=842)
    page.insert_text(
        (48, 48),
        f"{course['name']} · 第 {page_no} 页 / 共 {course['pages']} 页",
        fontsize=9,
        fontname="china-s",
    )
    page.insert_text((48, 92), chapter, fontsize=18, fontname="china-s")
    unique = UNIQUE_POINTS.get(course["name"])
    y = 130
    if unique and page_no in unique[0]:
        _, title, body = unique
        page.insert_text((48, y), f"【独有知识点】{title}", fontsize=13, fontname="china-s")
        y += 30
        for start in range(0, len(body), 46):
            page.insert_text((48, y), body[start : start + 46], fontsize=11, fontname="china-s")
            y += 24
        return
    for offset in range(5):
        topic = topics[(page_no + offset) % len(topics)]
        line = f"{topic}。判断时先确认适用条件，再对照原文说明结论。"
        for start in range(0, len(line), 46):
            page.insert_text((48, y), line[start : start + 46], fontsize=11, fontname="china-s")
            y += 22
        y += 4


def build(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for course in COURSES:
        doc = pymupdf.open()
        for page_no in range(1, int(course["pages"]) + 1):
            _add_page(doc, course, page_no)
        doc.set_toc([[1, title, start] for title, start in course["chapters"]])
        path = out_dir / course["name"]
        doc.save(path)
        doc.close()
        paths.append(path)
    return paths


def verify(paths: list[Path]) -> None:
    total = 0
    for path in paths:
        doc = pymupdf.open(path)
        total += doc.page_count
        doc.close()
    print(f"pages={total}")
    for (name, (pages, title, _)) in UNIQUE_POINTS.items():
        path = next(item for item in paths if item.name == name)
        doc = pymupdf.open(path)
        for page_no in pages:
            text = doc[page_no - 1].get_text()
            if title not in text:
                raise SystemExit(f"missing unique point {title} on {name} page {page_no}")
        doc.close()
        print(f"unique ok {name} pages {pages}: {title}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="acceptance_pdfs")
    args = parser.parse_args()
    paths = build(Path(args.out))
    verify(paths)
    print("written", args.out)


if __name__ == "__main__":
    main()