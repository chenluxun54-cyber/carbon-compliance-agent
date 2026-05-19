import pandas as pd
import numpy as np
from config import EXCEL_PATH, SHEET_COMPANY, LINEAR_INDICATORS


class DataLoader:

    def __init__(self):
        self._company_df = None

    def _load_excel(self):
        if self._company_df is not None:
            return
        self._company_df = pd.read_excel(
            EXCEL_PATH,
            sheet_name=SHEET_COMPANY,
            dtype={"company_id": str, "report_year": int}
        )
        for col in ["yoy_carbon_reduction_rate", "yoy_carbon_intensity_improve"]:
            if col in self._company_df.columns:
                self._company_df[col] = self._company_df[col].clip(lower=0)

    def _compute_rankings(self, industry: str, year: int) -> pd.DataFrame:
        group = self._company_df[
            (self._company_df["industry"] == industry) &
            (self._company_df["report_year"] == year)
        ].copy()
        sample_size = len(group)
        records = []

        for _, row in group.iterrows():
            record = {
                "company_id":  row["company_id"],
                "sample_size": sample_size,
            }
            for col, _, _ in LINEAR_INDICATORS:
                if col not in group.columns or group[col].isna().all():
                    record[f"rank_{col}"] = None
                    continue
                ranks = group[col].rank(
                    method="min", ascending=True, na_option="bottom"
                )
                record[f"rank_{col}"] = int(ranks.loc[row.name])
            records.append(record)

        return pd.DataFrame(records)

    def _clean(self, d: dict) -> dict:
        return {
            k: (None if (isinstance(v, float) and np.isnan(v)) else v)
            for k, v in d.items()
        }

    def fetch(self, company_id: str, report_year: int) -> dict:
        self._load_excel()
        mask = (
            (self._company_df["company_id"] == company_id) &
            (self._company_df["report_year"] == report_year)
        )
        matched = self._company_df[mask]
        if matched.empty:
            raise ValueError(
                f"未找到企业 {company_id} 在 {report_year} 年的数据"
            )
        company_data = self._clean(matched.iloc[0].to_dict())
        industry = company_data["industry"]

        rankings_df = self._compute_rankings(industry, report_year)
        row = rankings_df[rankings_df["company_id"] == company_id]
        if not row.empty:
            company_data.update(self._clean(row.iloc[0].to_dict()))

        return company_data

    def fetch_history(self, company_id: str, years: int = 3) -> list:
        self._load_excel()
        available = (
            self._company_df[self._company_df["company_id"] == company_id]
            ["report_year"]
            .sort_values(ascending=False)
            .head(years)
            .tolist()
        )
        return [self.fetch(company_id, yr) for yr in available]
