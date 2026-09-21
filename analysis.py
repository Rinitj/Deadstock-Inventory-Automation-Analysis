"""
Deadstock & Inventory Automation Dashboard
===========================================
Dataset: Retail Store Inventory Forecasting Dataset (Kaggle, synthetic)
Coverage: 5 stores x 20 products x 5 categories x 731 days (2022-01-01 to 2024-01-01)

Business questions:
  1. Which SKUs are overstocked / slow-moving ("deadstock risk") and how much
     working capital is tied up in them?
  2. Which SKUs are at risk of stocking out, and what should be reordered NOW?
  3. Can the reorder decision be automated instead of manually reviewed?

Note on methodology: this dataset does not contain SKUs with long stretches
of zero sales (a "classic" deadstock signal), so deadstock risk here is
defined using inventory turnover / days-of-cover, which is the standard
approach real inventory analysts use when a SKU never fully stops selling
but still moves too slowly relative to how much stock is held.

Author: Rinit Jain
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

sns.set_style("whitegrid")
plt.rcParams["figure.dpi"] = 110

LEAD_TIME_DAYS = 1          # assumption: replenishment lead time, in days.
                             # Calibrated from the data itself -- observed
                             # Days of Cover across SKUs averages ~1-2 days
                             # (fast-moving catalog, inventory capped at 500
                             # units vs. ~136 units/day average sales), so a
                             # 1-day lead time reflects this catalog's actual
                             # replenishment cadence rather than a generic
                             # textbook default.
SERVICE_LEVEL_Z = 1.65      # assumption: ~95% service level

# -----------------------------------------------------------------------
# 1. LOAD
# -----------------------------------------------------------------------
df = pd.read_csv("data/raw/retail_store_inventory.csv", parse_dates=["Date"])

# NOTE / data quality finding: in this synthetic dataset, the "Category"
# field is NOT a stable attribute of a Store+Product combination -- the
# same Store+Product shows different categories on different dates
# (e.g. S001/P0001 appears as Groceries, Furniture, Clothing, Toys and
# Electronics on different days). Since Store+Product IS consistently
# tracked every single day (731/731), we treat Store+Product as the true
# SKU, and assign each SKU its most frequently recorded category purely
# for reporting/labeling purposes.
df["SKU"] = df["Store ID"] + " | " + df["Product ID"]

sku_category = (
    df.groupby("SKU")["Category"]
    .agg(lambda x: x.mode().iloc[0])
    .rename("Representative_Category")
)

latest_date = df["Date"].max()
print(f"Data covers {df['Date'].min().date()} to {latest_date.date()}, "
      f"{df['SKU'].nunique()} SKUs across {df['Store ID'].nunique()} stores.")

# -----------------------------------------------------------------------
# 2. SKU-LEVEL SUMMARY: DEMAND, TURNOVER, DAYS OF COVER
# -----------------------------------------------------------------------
sku_summary = (
    df.groupby("SKU")
    .agg(
        Store=("Store ID", "first"),
        Product=("Product ID", "first"),
        Avg_Daily_Sales=("Units Sold", "mean"),
        Std_Daily_Sales=("Units Sold", "std"),
        Total_Units_Sold=("Units Sold", "sum"),
        Avg_Inventory_Level=("Inventory Level", "mean"),
        Avg_Price=("Price", "mean"),
        Days_Tracked=("Date", "count"),
    )
    .reset_index()
)
sku_summary = sku_summary.merge(sku_category, on="SKU", how="left")
sku_summary = sku_summary.rename(columns={"Representative_Category": "Category"})

# Latest known inventory level per SKU (most recent date on record — every
# SKU has a row on every date, so this join is now complete, no NaNs)
latest_inv = df[df["Date"] == latest_date][["SKU", "Inventory Level"]].rename(
    columns={"Inventory Level": "Current_Inventory"}
)
sku_summary = sku_summary.merge(latest_inv, on="SKU", how="left")

# Inventory turnover (annualized) and Days of Cover
sku_summary["Annual_Turnover"] = (
    sku_summary["Total_Units_Sold"] / sku_summary["Days_Tracked"] * 365
) / sku_summary["Avg_Inventory_Level"]

sku_summary["Days_of_Cover"] = (
    sku_summary["Current_Inventory"] / sku_summary["Avg_Daily_Sales"]
)

# Revenue contribution (for ABC classification)
sku_summary["Revenue"] = sku_summary["Total_Units_Sold"] * sku_summary["Avg_Price"]
sku_summary = sku_summary.sort_values("Revenue", ascending=False)
sku_summary["Cumulative_Revenue_Pct"] = (
    sku_summary["Revenue"].cumsum() / sku_summary["Revenue"].sum()
)

def abc_class(pct):
    if pct <= 0.80:
        return "A"
    elif pct <= 0.95:
        return "B"
    else:
        return "C"

sku_summary["ABC_Class"] = sku_summary["Cumulative_Revenue_Pct"].apply(abc_class)

# Deadstock-risk flag: top quartile of Days of Cover = slow-moving / overstocked
doc_threshold = sku_summary["Days_of_Cover"].quantile(0.75)
sku_summary["Deadstock_Risk"] = sku_summary["Days_of_Cover"] > doc_threshold
sku_summary["Value_Tied_Up"] = sku_summary["Current_Inventory"] * sku_summary["Avg_Price"]

# -----------------------------------------------------------------------
# 3. REORDER POINT & SAFETY STOCK (automation logic)
# -----------------------------------------------------------------------
sku_summary["Safety_Stock"] = (
    SERVICE_LEVEL_Z * sku_summary["Std_Daily_Sales"] * np.sqrt(LEAD_TIME_DAYS)
).round(0)
sku_summary["Reorder_Point"] = (
    sku_summary["Avg_Daily_Sales"] * LEAD_TIME_DAYS + sku_summary["Safety_Stock"]
).round(0)
sku_summary["Reorder_Now"] = sku_summary["Current_Inventory"] < sku_summary["Reorder_Point"]

sku_summary.to_csv("data/sku_inventory_summary.csv", index=False)

# -----------------------------------------------------------------------
# 4. AUTOMATED ALERT FILES (what a daily automation job would output)
# -----------------------------------------------------------------------
reorder_alerts = sku_summary[sku_summary["Reorder_Now"]][
    ["SKU", "Store", "Product", "Category", "Current_Inventory",
     "Reorder_Point", "Safety_Stock", "Avg_Daily_Sales"]
].sort_values("Current_Inventory")
reorder_alerts.to_csv("data/reorder_alerts.csv", index=False)

deadstock_report = sku_summary[sku_summary["Deadstock_Risk"]][
    ["SKU", "Store", "Product", "Category", "Days_of_Cover",
     "Annual_Turnover", "Current_Inventory", "Value_Tied_Up"]
].sort_values("Value_Tied_Up", ascending=False)
deadstock_report.to_csv("data/deadstock_report.csv", index=False)

print(f"\n{len(reorder_alerts)} SKUs need reordering now (out of {len(sku_summary)}).")
print(f"{len(deadstock_report)} SKUs flagged as deadstock risk, "
      f"tying up ${deadstock_report['Value_Tied_Up'].sum():,.0f} in inventory value.")
print(f"Total inventory value across all SKUs: "
      f"${sku_summary['Value_Tied_Up'].sum():,.0f}")
print(f"ABC breakdown:\n{sku_summary['ABC_Class'].value_counts()}")

# -----------------------------------------------------------------------
# 5. CHARTS
# -----------------------------------------------------------------------
# 5a. Days of Cover distribution with deadstock threshold
plt.figure(figsize=(8, 5))
sns.histplot(sku_summary["Days_of_Cover"], bins=25, color="#2c7fb8")
plt.axvline(doc_threshold, color="#d73027", linestyle="--",
             label=f"Deadstock-risk threshold ({doc_threshold:.1f} days)")
plt.title("Distribution of Days of Inventory Cover Across SKUs")
plt.xlabel("Days of Cover (Current Inventory / Avg Daily Sales)")
plt.legend()
plt.tight_layout()
plt.savefig("charts/01_days_of_cover_distribution.png")
plt.close()

# 5b. Value tied up in deadstock-risk SKUs by category
cat_deadstock = (
    deadstock_report.groupby("Category")["Value_Tied_Up"].sum().sort_values()
)
plt.figure(figsize=(7, 4.5))
cat_deadstock.plot(kind="barh", color="#d95f0e")
plt.title("Inventory Value Tied Up in Deadstock-Risk SKUs, by Category")
plt.xlabel("$ Value Tied Up")
plt.tight_layout()
plt.savefig("charts/02_deadstock_value_by_category.png")
plt.close()

# 5c. Top 10 overstocked SKUs by value tied up
top10 = deadstock_report.nlargest(10, "Value_Tied_Up")
plt.figure(figsize=(8, 5))
plt.barh(top10["SKU"], top10["Value_Tied_Up"], color="#d73027")
plt.gca().invert_yaxis()
plt.title("Top 10 Overstocked SKUs by Value Tied Up")
plt.xlabel("$ Value Tied Up")
plt.tight_layout()
plt.savefig("charts/03_top10_overstocked_skus.png")
plt.close()

# 5d. ABC classification summary
abc_summary = sku_summary.groupby("ABC_Class").agg(
    SKU_Count=("SKU", "count"), Revenue=("Revenue", "sum")
)
fig, axes = plt.subplots(1, 2, figsize=(10, 4.5))
axes[0].pie(abc_summary["SKU_Count"], labels=abc_summary.index, autopct="%1.0f%%",
             colors=["#2c7fb8", "#66c2a5", "#fc8d62"])
axes[0].set_title("Share of SKUs by ABC Class")
axes[1].pie(abc_summary["Revenue"], labels=abc_summary.index, autopct="%1.0f%%",
             colors=["#2c7fb8", "#66c2a5", "#fc8d62"])
axes[1].set_title("Share of Revenue by ABC Class")
plt.tight_layout()
plt.savefig("charts/04_abc_classification.png")
plt.close()

# 5e. Reorder alert snapshot — current inventory vs reorder point
plt.figure(figsize=(9, 5.5))
plot_data = reorder_alerts.head(15)
y_pos = np.arange(len(plot_data))
plt.barh(y_pos, plot_data["Reorder_Point"], color="#fee08b", label="Reorder Point")
plt.barh(y_pos, plot_data["Current_Inventory"], color="#d73027", label="Current Inventory")
plt.yticks(y_pos, plot_data["SKU"])
plt.gca().invert_yaxis()
plt.xlabel("Units")
plt.title("Reorder Alerts: Current Stock vs Reorder Point (most urgent 15)")
plt.legend()
plt.tight_layout()
plt.savefig("charts/05_reorder_alerts.png")
plt.close()

print("\nDone. Charts saved to /charts, alert CSVs saved to project root.")
