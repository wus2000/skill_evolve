---
license: cc-by-sa-4.0
task_categories:
- table-question-answering
- question-answering
language:
- en
size_categories:
- 1K<n<10K
---
# BIRD-SQL Train (Filtered)

A high-quality subset of the original BIRD train split for text-to-SQL finetuning.

## Overview

Over the past year the community has shared many observations about data quality in BIRD. We performed a rigorous data quality check process to retain examples that are **consistent with schema** and **faithfully answer the question**. The resulting set keeps **6,601** instances out of **9,428** (≈70%), and serves as a drop-in replacement for training.

- **Original Train:** 9,428
- **Filtered Train (this release):** 6,601

The example code for training and inference can be found [here](https://github.com/bird-bench/mini_dev/tree/main)

### For New Users
If you are new to BIRD project, you can download the complete databases for the training set using the following link:
[Download BIRD Train](https://bird-bench.oss-cn-beijing.aliyuncs.com/train.zip)

### For Existing Users
If you have already downloaded the BIRD training databases, you can pull the latest filtered data updates through Hugging Face using the following scripts:

```python
from datasets import load_dataset
# Load the dataset
dataset = load_dataset("birdsql/bird23-train-filtered")
# Access the dataset
print(dataset["train"][0])
```

You can find the column meaning json file [here](https://huggingface.co/datasets/birdsql/bird23-train-filtered/resolve/main/train_column_meaning.json) the key is composed of `database_id|table_name|column_name`, and the value is key information about each column and their value summarized from raw CSVs.

## Training Quality
We validate by finetuning a single open model with a standard SFT recipe and evaluating on the official BIRD Mini Dev and Dev set. We use the [Qwen/Qwen2.5-3B](https://huggingface.co/Qwen/Qwen2.5-3B) as the base model and follow the [Arctic-Text2SQL-R1 project ](https://www.snowflake.com/en/product/ai/ai-research/) data processing. You can find the original [repo](https://github.com/snowflakedb/ArcticTraining/tree/main/projects/arctic_text2sql_r1) and [paper](https://arxiv.org/abs/2505.20315) here. 

### Performance Comparison (EX)

| Setting                   | Mini-Dev | Dev      |
| ------------------------- | -------- | -------- |
| **Baseline**              | 26.2     | 31.88    |
| **Original Train** | 45.4     | 50.46    |
| **Filtered Train** | **46.0** | **50.0** |

Takeaway: with **~30%** fewer training items, the filtered set matches the full set on the Mini-Dev and Dev set.

### Data scaling on the filtered set


<p align="left">
  <img src="https://cdn-uploads.huggingface.co/production/uploads/653693cb8ee17cfd44eed8ce/6_0KJzy4o1GfMDnqP3nA1.png" width="520">
</p>

## Dataset Introduction

The dataset contains the main following resources:

- `database`: The database should be stored under the [`./train_databases/`](./train_databases/). In each database folder, it has two components:
  - `database_description`: the csv files are manufactured to describe database schema and its values for models to explore or references.
  - `sqlite`: The database contents in BIRD.
- `data`: Each text-to-SQL pairs with the oracle knowledge evidence is stored as a JSONL file, i.e., `train.jsonl`. It has four main parts:
  - `db_id`: the names of databases
  - `question`: the questions curated by human crowdsourcing according to database descriptions, database contents.
  - `evidence`: the external knowledge evidence annotated by experts for assistance of models or SQL annotators.
  - `SQL`: SQLs annotated by crowdsource referring to database descriptions, database contents, to answer the questions accurately.

## Acknowledgements
This work builds on the BIRD benchmark and the efforts of its creators and contributors. We thank the community for continuous feedback that helped shape this release.



## Citation
Please cite the repo if you think our work is helpful to you.
```
@article{li2024can,
  title={Can llm already serve as a database interface? a big bench for large-scale database grounded text-to-sqls},
  author={Li, Jinyang and Hui, Binyuan and Qu, Ge and Yang, Jiaxi and Li, Binhua and Li, Bowen and Wang, Bailin and Qin, Bowen and Geng, Ruiying and Huo, Nan and others},
  journal={Advances in Neural Information Processing Systems},
  volume={36},
  year={2024}
}
```