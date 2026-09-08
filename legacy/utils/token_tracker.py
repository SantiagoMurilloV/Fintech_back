"""Token usage tracker (Tasman pattern) — CU is expensive, so everything is measured."""

from collections import defaultdict

# USD per 1M tokens (input, output) — adjust as pricing changes
PRICING = {
    "claude-opus-4-5": (5.00, 25.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "deepseek-chat": (0.27, 1.10),
}


class TokenTracker:
    def __init__(self):
        self.calls = []
        self.totals = defaultdict(lambda: {"input": 0, "output": 0})

    def track(self, model: str, input_tokens: int, output_tokens: int, call_type: str = ""):
        self.calls.append(
            {"model": model, "input": input_tokens, "output": output_tokens, "type": call_type}
        )
        self.totals[model]["input"] += input_tokens
        self.totals[model]["output"] += output_tokens

    def cost_usd(self) -> float:
        total = 0.0
        for model, t in self.totals.items():
            pin, pout = PRICING.get(model, (0.0, 0.0))
            total += t["input"] / 1e6 * pin + t["output"] / 1e6 * pout
        return total

    def summary(self) -> str:
        lines = ["── Token usage ──"]
        for model, t in self.totals.items():
            lines.append(f"{model}: {t['input']:,} in / {t['output']:,} out")
        lines.append(f"Estimated cost: ${self.cost_usd():.4f} USD ({len(self.calls)} calls)")
        return "\n".join(lines)


tracker = TokenTracker()
