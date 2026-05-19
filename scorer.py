from config import LINEAR_INDICATORS, DIRECT_SCORING_RULES, DIMENSIONS


class CarbonScorer:

    _MAX_SCORES = {col: ms for col, ms, _ in LINEAR_INDICATORS}
    _MAX_SCORES.update({
        "certification_type":         2,
        "carbon_target_completeness": 1,
        "disclosure_level":           3,
        "supply_chain_disclosure":    1,
        "data_submission_lag_months": 1,
    })

    def _score_linear(self, col, rank, sample_size, is_inverse):
        max_score = self._MAX_SCORES[col]
        if rank is None or not sample_size:
            return 0.0, 0.0
        percentile = rank / sample_size
        if is_inverse:
            percentile = 1 - percentile
        return round(max_score * percentile, 1), round(percentile, 4)

    def _score_direct(self, col, value):
        if col == "data_submission_lag_months":
            if value is None:
                return 0.0
            lag = int(value)
            if lag <= 3:  return 1.0
            if lag <= 6:  return 0.7
            if lag <= 12: return 0.3
            return 0.0
        rules = DIRECT_SCORING_RULES.get(col, {})
        return rules.get(str(value).strip() if value else "none", 0.0)

    def score(self, data: dict) -> dict:
        sample_size = data.get("sample_size")
        indicator_scores = {}
        flags = []

        # 线性赋分
        for col, max_score, is_inverse in LINEAR_INDICATORS:
            rank = data.get(f"rank_{col}")

            if col == "water_recycling_rate" and data.get(col) is None:
                rank = round(sample_size / 2) if sample_size else None
                flags.append("4.2 工业用水重复利用率：数据缺失，已用行业中位数替代")

            if data.get(col) is None and col != "water_recycling_rate":
                indicator_scores[col] = {
                    "score": 0.0, "max_score": max_score,
                    "percentile": 0.0, "method": "linear",
                    "direction": "inverse" if is_inverse else "positive",
                    "missing": True
                }
                flags.append(f"{col}：数据缺失，得 0 分")
                continue

            score, pct = self._score_linear(col, rank, sample_size, is_inverse)
            indicator_scores[col] = {
                "score": score, "max_score": max_score,
                "percentile": pct, "method": "linear",
                "direction": "inverse" if is_inverse else "positive",
                "missing": False
            }

        # 直接赋分
        for col in ["certification_type", "carbon_target_completeness",
                    "disclosure_level", "supply_chain_disclosure",
                    "data_submission_lag_months"]:
            score = self._score_direct(col, data.get(col))
            indicator_scores[col] = {
                "score": score, "max_score": self._MAX_SCORES[col],
                "method": "direct", "value": data.get(col),
                "missing": data.get(col) is None
            }

        # 维度汇总
        dimensions = []
        total = 0.0
        for dim in DIMENSIONS:
            dim_score = round(sum(
                indicator_scores.get(i, {}).get("score", 0.0)
                for i in dim["indicators"]
            ), 1)
            total += dim_score
            dimensions.append({
                "id": dim["id"], "name": dim["name"],
                "score": dim_score, "max_score": dim["max_score"],
                "percentage": round(dim_score / dim["max_score"] * 100, 1),
                "indicators": {
                    i: indicator_scores[i]
                    for i in dim["indicators"] if i in indicator_scores
                }
            })

        return {
            "company_id":   data.get("company_id"),
            "company_name": data.get("company_name"),
            "industry":     data.get("industry"),
            "report_year":  data.get("report_year"),
            "sample_size":  sample_size,
            "total_score":  round(total, 1),
            "dimensions":   dimensions,
            "flags":        flags,
        }
