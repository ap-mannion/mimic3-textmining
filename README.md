# mimic3-textmining
Clinical text-mining/machine learning project I did as part of my masters thesis at Laboratoire Informatique de Grenoble.

# Dependencies
* Scikit-learn https://github.com/scikit-learn/scikit-learn
* Pytorch https://github.com/pytorch/pytorch
* Transformers https://github.com/huggingface/transformers
* Pytorch Lightning https://github.com/PyTorchLightning/pytorch-lightning
* Gensim https://github.com/RaRe-Technologies/gensim
* NLTK https://github.com/nltk/nltk

# The Dataset
MIMIC-III (Medical Information Mart for Intensive Care) is a large, freely available database comprising deidentified medical
information pertaining to patients admitted to the Beth Israel Deaconess Medical Center in Boston, Massachusetts, USA. It was compiled and released 
as an open-source medical data resource to aid in the reproducibility of clinical studies which use electronic health records. It is available to
researchers subject to a data use agreement, hosted at Physionet.org and is unique in that it is the only openly accessible dataset
of its kind. The patient information therein spans a period of over ten years. The iteration of the database used in these experiments is
version 1.4.

Although MIMIC-III contains a wide range of continuously monitored physiological measurements, these experiments consider only the textual information
contained in the progress notes documented by care providers, modelling patient trajectories simply as a series of notes and aggregating across the embeddings
of these notes to train predictive algorithms. The database contains a total of 26 relational
tables (see https://mit-lcp.github.io/mimic-schema-spy/index.html), of which only three were necessary for these experiments.
The dataset contains information for a total of 46,520 patients, of which 38,983 have one single admission. Ordinarily, only patients with two or more
admissions are used for trajectory prediction, but the formulation of the reaccess prediction problem used in these experiments
(for MIMIC-III, the variable is in fact ICU readmission) permits the use of all of the available data, negatively labelling all single-admission patients.

# Text Processing & Concept Extraction
It is a common strategy in the field of biomedical and clinical NLP to improve the utility of text embeddings using annotations from medical knowledge bases.
To test whether or not text annotation could be useful for this work, the concept extraction tool QuickUMLS was used to replace any terms found in the MIMIC-III
clinical notes with certain results from the UMLS MetaThesaurus, which contains a unification of many different biomedical corpora.

The goal of concept extraction in this context is to eliminate noise from the input data by identifying relevant concepts and removing words that (in theory)
do not contain information relevant to the target task. This reduces the extent to which the text data is unstructured, as it is mapped to variable-length
sequences of components belonging to a structured, finite (although extremely large) terminology network.

Two different types of concept mappings were used; the preferred term of each biomedical concept, and its CUI (Concept Unique Identifier), an identification code system used by UMLS to index concepts across different vocabularies. For each biomedical concept extracted from the text, the \textit{semantic type} identifier was also documented - this is another UMLS identification system denoting the category into which the concept falls (e.g. pharmacological substance, anatomical structure, etc.). Thus, five versions of the MIMIC-III text corpus were experimented with, listed below;

* The original text
* The preferred terms
* The preferred terms with semantic type identifiers concatenated to the end of the notes
* Concept Unique Identifiers
* Concept Unique Identifiers with semantic types
