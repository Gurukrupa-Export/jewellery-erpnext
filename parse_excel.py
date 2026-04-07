import pandas as pd
import json
with open("gk_flow_extracted.json", "w") as f:
    data = {k: v.to_dict(orient="records") for k, v in pd.read_excel("/home/dhinesh/Downloads/GK new flow.xlsx", sheet_name=None).items()}
    json.dump(data, f, indent=2, default=str)
