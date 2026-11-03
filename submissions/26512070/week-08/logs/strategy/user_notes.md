# 2026-11-04 02:2x — restock: breadth or depth?

The user's goal: win back the early loss by selling many units at a profit (strategy v19, first priority).

Question: should the buy list go wide (more of the market's best-selling SKUs, a few units each) instead of deep
(a few SKUs we missed, up to 8 units each)? Decide it now with the playbook numbers.

What we saw:
- House buyers post "침구 구합니다, 예산 91만 원대, 파는 가게가 없네요" (#1345, #1330, #1356). Each order accepts only
  certain SKUs of its category, and the kit shows a buyer only the shops that hold one of those SKUs. A shop without
  them is never visited, so these orders never show in me.missed. Our forum reply (#1349, Ge62449c9) does not help
  when that SKU is not in the order's list.
- The market's best sellers are the SKUs orders accept: 침구 sold 1,600+ in 6 h at a median 75% of list; G0be9ed07
  (소파패드) 772 sold, G07ec17d5 (매트리스) 675.
- The buy list now: our missed SKUs first (apron G53e73f8c 39 asks in 2 h, Ge62449c9 18), market top 5 x 2 units.
  At 02:17 the cash we may spend is ~2.33M but the stock cap leaves ~0.93M, and 4 mattresses (G0f415407, worst cost
  ~199k each) take most of it, so no market best seller gets bought this window.

Playbook numbers you can set (playbook.restock): cover_windows (0.5-6), market_top (0-30), market_units (0-8),
max_line_share (0.05-1: the most one line may take of the window's money; 1 = no cap). Stock cap: limits.max_stock_share
(at most 0.5 by yourself; the user set 0.6).
