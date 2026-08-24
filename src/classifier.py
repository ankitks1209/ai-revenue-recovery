from typing import Dict, Any, List
import pandas as pd
from sklearn.metrics import classification_report
from src.config import load_taxonomy

class FailureClassifier:
    def __init__(self, taxonomy_path: str = None):
        """
        Initialize classifier by loading the failure code taxonomy from configuration.
        Constructs a fast lookup dictionary mapping normalized failure codes to categories.
        """
        self.taxonomy = load_taxonomy()
        self.code_to_category = self._build_lookup_table(self.taxonomy)

    def _build_lookup_table(self, taxonomy_data: dict) -> Dict[str, str]:
        """
        Builds a mapping from failure_code -> root_cause_category based on taxonomy.yaml.
        Normalizes codes to lowercase strings.
        """
        lookup = {}
        categories = taxonomy_data.get("categories", {})
        for category, details in categories.items():
            codes = details.get("codes", [])
            for code in codes:
                # Normalize code to string, lowercase, stripped
                norm_code = str(code).strip().lower()
                lookup[norm_code] = category
        return lookup

    def classify_code(self, failure_code: str) -> str:
        """
        Map a raw failure code to a root-cause category.
        Unknown or unmapped codes are routed to 'Unknown / Ambiguous'.
        """
        if not failure_code:
            return "Unknown / Ambiguous"
        
        norm_code = str(failure_code).strip().lower()
        return self.code_to_category.get(norm_code, "Unknown / Ambiguous")

    def classify_batch(self, records: List[Dict[str, Any]]) -> pd.DataFrame:
        """
        Classify a batch of records, adding a 'predicted_category' column.
        """
        df = pd.DataFrame(records)
        if df.empty:
            df["predicted_category"] = []
            return df

        df["predicted_category"] = df["failure_code"].apply(self.classify_code)
        return df

    def evaluate_batch(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Evaluate classifier predictions against ground truth root_cause_label.
        Returns evaluation metrics including classification report and misclassifications dataframe.
        """
        df = self.classify_batch(records)
        
        y_true = df["root_cause_label"]
        y_pred = df["predicted_category"]

        # Generate scikit-learn classification report (as dict and string)
        report_dict = classification_report(y_true, y_pred, output_dict=True, zero_division=0)
        report_str = classification_report(y_true, y_pred, zero_division=0)

        # Identify misclassifications
        misclassified_df = df[df["root_cause_label"] != df["predicted_category"]]

        return {
            "dataframe": df,
            "report_dict": report_dict,
            "report_str": report_str,
            "misclassifications": misclassified_df
        }

if __name__ == "__main__":
    from src.generator import generate_failed_payments
    classifier = FailureClassifier()
    records = generate_failed_payments(count=60)
    eval_res = classifier.evaluate_batch(records)
    print("=== Day 2 Classifier Evaluation Report ===")
    print(eval_res["report_str"])
    print(f"Misclassified count: {len(eval_res['misclassifications'])}")
