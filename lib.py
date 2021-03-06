import torch
import torch.utils.data as data
from torch.optim import SGD
from torch.optim.lr_scheduler import CyclicLR
from torch.nn import CrossEntropyLoss
import numpy as np
import pytorch_lightning as pl
import pytorch_lightning.metrics.classification as M
from pandas import DataFrame, read_csv, isna
from nltk import word_tokenize
from collections import OrderedDict
from gensim.models import Word2Vec
from sklearn.pipeline import Pipeline
from sklearn.model_selection import GridSearchCV, StratifiedKFold
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import precision_score, recall_score, f1_score, roc_auc_score
from transformers import BertTokenizer, BertForSequenceClassification, AdamW
from math import ceil
from random import sample
from util import load_txt_df
from pkg_resources import parse_version


###################################
# WORD2VEC IMPLEMENTATION CLASSES #
###################################

class W2VEmbedAggregate(object):
    '''Object that implements scikit-learn style methods to compute Word2Vec embeddings
    and aggregate across notes/patients'''

    def __init__(self, patient_ids, **kwargs):
        self.patient_ids = patient_ids
        self.arg_names = ['sg', 'size', 'window', 'min_count', 'alpha', 'iter', 'workers']

    def set_params(self, **kwargs):
        self.__dict__.update((k, v) for k, v in kwargs.items() if k in self.arg_names)

    def fit(self, text, y=None):
        '''Trains a Word2Vec model: only to be called internally in the fit() method of the grid search'''
        w2v_kwargs = {}
        w2v_kwargs.update((k, v) for k, v in self.__dict__.items() if k in self.arg_names)
        self.embedding = Word2Vec(text, **w2v_kwargs)

        return self

    def transform(self, text, assign_to_attr=True):
        '''Aggregates over all word embeddings in each note'''
        word_vectors = self.embedding.wv
        dim = word_vectors.vector_size
        n = len(text)

        # aggregate the word vectors for each note (concatenation)
        _concat_aggreg = lambda arr: np.concatenate(
            (np.mean(arr, 0), np.max(arr, 0), np.min(arr, 0))
        )
        X = np.empty((n, dim*3))
        for i in range(n):
            note_list = []
            for word in text[i]:
                try:
                    note_list.append(word_vectors[word])
                except KeyError:
                    continue
            if len(note_list) == 0:
                # even if no word vectors are found the output matrix still needs to be the right shape
                X[i,:] = np.zeros(dim*3)
            else:
                note_array = np.array(note_list).reshape((len(note_list), dim))
                X[i,:] = _concat_aggreg(note_array)

        if assign_to_attr:
            self.note_level_aggregations = X

        return X


class MIMICWord2VecReadmissionPredictor(object):
    '''Implementation class for the ML pipeline that goes from the cleaned/annotated
    text -> word embeddings -> SVM classification'''

    def __init__(self, txtvar, st_aug, seed=1, train_chunksize=1e5, test_chunksize=1e3, db=False):
        self.txtvar = txtvar
        self.st_aug = st_aug
        self.seed = seed
        self.train_chunksize = train_chunksize
        self.test_chunksize = test_chunksize
        self.db = db

    def _load_data(self, corpus_fp, readm_fp, chunksize=None, adapt_for_gridsearch=False):
        cols = ['SUBJECT_ID', 'HADM_ID', self.txtvar]
        dtypes = {}
        for c in cols:
            dtypes[c] = str
        if self.st_aug:
            cols.append('SEMTYPES')
            dtypes['SEMTYPES'] = str
        corpus_df = read_csv(
            corpus_fp,
            index_col=0,
            usecols=cols,
            dtype=dtypes,
            chunksize=chunksize
        )
        readm_df = read_csv(readm_fp, index_col=0)
        patient_ids = []
        text = []
        readm_df = read_csv(readm_fp, index_col=0)
        for chunk in corpus_df:
            chunk = chunk[chunk.index.isin(readm_df.index)]
            patient_ids += chunk.index.get_level_values(0).tolist()
            if sum(isna(chunk[self.txtvar])) > 0:
                chunk[self.txtvar] = chunk[self.txtvar].fillna('')
            if self.st_aug:
                chunk = chunk.assign(
                    **{self.txtvar:chunk[self.txtvar]+chunk.SEMTYPES.fillna('')}
                )
                chunk.drop('SEMTYPES', axis=1, inplace=True)
            for note in chunk[self.txtvar]:
                text.append(word_tokenize(note))
            if self.db:
                break

        # labels have to be the same length as train data for the pipeline
        # make sure that labels are ordered according to the patient IDs in the text dataset
        readm_df = readm_df[readm_df.index.isin(patient_ids)]
        input_labels = readm_df.READM.values
        readm_ordering_map = {}
        for p_id, label in zip(readm_df.index, input_labels):
            readm_ordering_map[p_id] = label

        # order the labels to be correspond to the output of patient aggregations
        labels = []
        unique_id_list = list(dict.fromkeys(patient_ids))
        for p_id in unique_id_list:
            labels.append(input_labels[readm_df.index.tolist().index(p_id)])

        if adapt_for_gridsearch:
            gridsearch_labels = []
            for p_id in patient_ids:
                try:
                    gridsearch_labels.append(readm_ordering_map[p_id])
                except KeyError:
                    continue

            ret = tuple(np.array(patient_ids), text, np.array(labels), np.array(gridsearch_labels))
        else:
            ret = tuple(np.array(patient_ids), text, np.array(labels))

        return ret

    def _load_train_data(self, corpus_fp, readm_fp, chunksize, adapt_for_gridsearch):
        self.train_patient_ids, self.train_text, self.train_labels, self.gridsearch_labels =\
            self._load_data(
                corpus_fp=corpus_fp, readm_fp=readm_fp, chunksize=chunksize, adapt_for_gridsearch=adapt_for_gridsearch
            )

    def _load_test_data(self, corpus_fp, readm_fp):
        self.test_patient_ids, self.test_text, self.test_labels =\
            self._load_data(corpus_fp=corpus_fp, readm_fp=readm_fp, chunksize=1e3)

    def choose_params(self, corpus_fp, readm_fp, n_jobs, use_multithreading=False):
        self._load_train_data(
            corpus_fp=corpus_fp, readm_fp=readm_fp, chunksize=self.train_chunksize, adapt_for_gridsearch=True
        )
        pipeline = Pipeline(
            steps = [
                ('embed_agg', W2VEmbedAggregate(patient_ids=self.train_patient_ids)),
                ('clf', SGDClassifier(random_state=self.seed))
            ]
        )
        lr_grid = [10**i for i in range(-4, -1)]
        param_grid = [
            {
                'embed_agg__sg':[1, 0],
                'embed_agg__size':[100, 200],
                'embed_agg__window':[5, 7, 9],
                'embed_agg__alpha':lr_grid,
                'clf__alpha':lr_grid
            }
        ]
        grid_w2v_sgd = GridSearchCV(
            pipeline,
            param_grid=param_grid,
            refit=True,
            n_jobs=n_jobs,
            scoring='roc_auc',
            cv=StratifiedKFold(n_splits=5, shuffle=True, random_state=self.seed)
        )
        if use_multithreading:
            from sklearn.utils import parallel_backend

            with parallel_backend('threading'):
                gridsearch_res = grid_w2v_sgd.fit(self.train_text, self.gridsearch_labels)
        else:
            gridsearch_res = grid_w2v_sgd.fit(self.train_text, self.gridsearch_labels)

        self.w2v_agg_model = gridsearch_res.best_estimator_.named_steps['embed_agg']
        self.clf = gridsearch_res.best_estimator_.named_steps['clf']

    def _patient_aggregation(self, note_vectors, labels=None):
        dim = self.w2v_agg_model.embedding.wv.vector_size
        patient_to_nv_map = {}
        patient_labels = []
        if labels is not None:
            for p_id, vector, lab in zip(self.train_patient_ids, note_vectors, labels):
                if p_id not in patient_to_nv_map:
                    patient_to_nv_map[p_id] = vector
                    patient_labels.append(lab)
                else:
                    patient_to_nv_map[p_id] = np.vstack((patient_to_nv_map[p_id], vector))
        else:
            for p_id, vector in zip(self.train_patient_ids, note_vectors):
                if p_id not in patient_to_nv_map:
                    patient_to_nv_map[p_id] = vector
                else:
                    patient_to_nv_map[p_id] = np.vstack((patient_to_nv_map[p_id], vector))
        X = np.empty((len(patient_to_nv_map), dim))
        i = 0
        for array in patient_to_nv_map.values():
            X[i,:] = np.mean(array, 0)
            i += 1

        return X, np.array(patient_labels)

    def train(self, text=None, labels=None, out_fp=None):
        if text is not None and labels is not None:
            # TODO assume we're running the full pipeline from scratch and instantiate a new embed-and-aggregate object
            pass
        else:
            # get note-level vectors and take the mean over them for each patient
            X, _ = self._patient_aggregation(
                self.w2v_agg_model.note_level_aggregations
            )

            # run a finer-tuned search for the best learning rate for the classifier using patient-level classifications
            current_lr = self.clf.alpha
            lr_grid = [current_lr*x for x in [0.5, 1.0, 2.0]]
            grid_sgd = GridSearchCV(
                SGDClassifier(random_state=self.seed),
                param_grid={'alpha':lr_grid},
                refit=True,
                n_jobs=-1,
                scoring='roc_auc',
                cv=StratifiedKFold(n_splits=5, random_state=self.seed)
            )
            alpha_gridsearch_res = grid_sgd.fit(X, self.train_labels)
            if alpha_gridsearch_res.best_estimator_.alpha != current_lr:
                self.clf.alpha = alpha_gridsearch_res.best_estimator_.alpha

            if out_fp is not None:
                dev_pred = self.clf.predict(X)
                dev_score = self.clf.decision_function(X)
                dev_prec = precision_score(self.train_labels, dev_pred, average='weighted')
                dev_recall = recall_score(self.train_labels, dev_pred, average='weighted')
                dev_f1 = f1_score(self.train_labels, dev_pred, average='weighted')
                dev_auroc = roc_auc_score(self.train_labels, dev_score, average='weighted')
                with open(out_fp, 'w+') as model_desc:
                    model_desc.write(
                        f'''Word2Vec-based test results\nVariable {self.txtvar}
Semantic types: {'yes' if self.st_aug else 'no'}\n\n---\nPipeline\n---{self.w2v_agg_model}
{self.clf}\nW2V Params:
{'skip-gram' if self.w2v_agg_model.embedding.sg == 1 else 'CBOW'}
W2V LR {self.w2v_agg_model.embedding.alpha}
SGD LR {self.w2v_agg_model.alpha}
dim {self.w2v_agg_model.embedding.wv.vector_size}
window {self.w2v_agg_model.embedding.window}
epochs {self.w2v_agg_model.embedding.iter}\n-- DEVELOPMENT RESULTS --
\n---\nScores\n---\nPrecision {dev_prec}, Recall {dev_recall}, F1 = {dev_f1}, AUROC {dev_auroc}'''
                        )


    def test(self, corpus_fp, readm_fp, out_fp=None, save_model_fp=None):
        self._load_test_data(corpus_fp, readm_fp)
        self.w2v_agg_model.embedding.train(
            self.test_text, total_examples=len(self.test_text), epochs=5
        )
        X, agg_test_labels = self._patient_aggregation(
            self.w2v_agg_model.transform(self.test_text, assign_to_attr=False),
            labels=self.test_labels
        )
        test_pred = self.clf.predict(X)
        test_scores = self.clf.decision_function(X)
        test_prec = precision_score(agg_test_labels, test_pred)
        test_recall = recall_score(agg_test_labels, test_pred)
        test_f1 = f1_score(agg_test_labels, test_pred)
        test_auroc = roc_auc_score(agg_test_labels, test_scores)

        if out_fp is not None:
            with open(out_fp, 'w+') as model_desc:
                model_desc.write(
                    f'''Word2Vec-based test results\nVariable {self.txtvar}
Semantic types: {'yes' if self.st_aug else 'no'}\n\n---\nPipeline\n---{self.w2v_agg_model}
{self.clf}\nW2V Params:
{'skip-gram' if self.w2v_agg_model.embedding.sg == 1 else 'CBOW'}
LR {self.w2v_agg_model.embedding.alpha}
dim {self.w2v_agg_model.embedding.wv.vector_size}
window {self.w2v_agg_model.embedding.window}
epochs {self.w2v_agg_model.embedding.iter}\n-- TEST RESULTS --
\n---\nScores\n---\nPrecision {test_prec}, Recall {test_recall}, F1 = {test_f1}, AUROC {test_auroc}'''
                    )

        if save_model_fp is not None:
            self.w2v_agg_model.embedding.save(save_model_fp)


###############################
# BERT IMPLEMENTATION CLASSES #
###############################

class EncodedDataset(data.Dataset):
    '''Pytorch-inherited dataset class that tokenizes the text batch-by-batch as
    it is passed to the dataloader to save RAM'''

    def __init__(self, df, bert_model, txtvar, seq_len):
        tokenizer = BertTokenizer.from_pretrained(bert_model, do_lower_case=True)

        # encoding has to be done during the initialisation, because torch expects all of the tensors output by __getitem__() to
        # be of the same size, so I can't return a stack of sequences for a patient index, it has to be a single sequence for the
        # sequence index
        input_ids, attn_masks, labels, patient_ids = [], [], [], []
        for patient, note, label in zip(df.SUBJECT_ID, df[txtvar], df.READM):
            encoding = tokenizer.encode_plus(
                note,
                add_special_tokens=True,
                truncation=True,
                max_length=seq_len,
                pad_to_max_length=True,
                return_attention_mask=True,
                return_tensors='pt',
                return_overflowing_tokens=True
            )
            input_ids.append(encoding['input_ids'].reshape(seq_len))
            attn_masks.append(encoding['attention_mask'].reshape(seq_len))
            patient_ids.append(patient)
            labels.append(label)

            if 'overflowing_tokens' in encoding.keys():
                overflow = encoding['overflowing_tokens']
                n_overflow = len(overflow)
                # split the overflowing tokens into sequences of size 512 and label them
                # with the current subject identifier and label
                for i in range(ceil(n_overflow/seq_len)):
                    # duplicate ID and label for each sequence
                    patient_ids.append(patient)
                    labels.append(label)
                    overflow_seq = overflow[seq_len*i:seq_len*(i+1)]
                    overflow_seq_len = overflow_seq.shape[1]
                    if overflow_seq_len == seq_len:
                        attn_mask = torch.ones(seq_len)
                    else:
                        overflow_seq = torch.cat(
                            (overflow_seq, torch.zeros(seq_len-overflow_seq_len))
                        )
                        attn_mask = torch.cat(
                            (torch.ones(overflow_seq_len), torch.zeros(seq_len-overflow_seq_len))
                        )
                    input_ids.append(overflow_seq.long())
                    attn_masks.append(attn_mask.long())

        self.input_ids = input_ids
        self.attn_masks = attn_masks
        self.labels = labels
        self.patient_ids = patient_ids

    def __len__(self):
        return len(self.patient_ids)

    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()

        return {
            'input_ids' : self.input_ids[idx],
            'attn_masks' : self.attn_masks[idx],
            'labels' : torch.tensor(self.labels[idx]),
            'patient_ids' : torch.tensor(self.patient_ids[idx])
        }


class MIMICBERTReadmissionPredictor(pl.LightningModule):
    '''This class implements model hooks into the Pytorch-Lightning framework, basically
    a wrapper around Pytorch module functionality'''

    def __init__(self, **kwargs):
        super().__init__()

        if parse_version(pl.__version__) < parse_version('0.8.1'):
            raise RuntimeError('''This implementation requires Pytorch-Lightning version
                0.8.1 or later''')

        params = [
            'n_train_fp', 'r_train_fp', 'n_test_fp', 'r_test_fp', # data file paths
            'val_frac', 'batch_size', 'threads', 'optimiser', # implementation arguments
            'bert_model', 'txtvar', 'sequence_len', 'st_aug', 'proba_aggregation_scale_factor', # language-model arguments
            'db', # boolean - debug mode
            'write_test_results_to', 'write_dev_results_to',
            'update_all_params', # bool: update the entire BERT model rather than just fine-tuning the final layer
            'verbose' #bool
        ]

        # default arguments
        self.optimiser = 'sgd'
        self.threads = torch.get_num_threads()
        self.proba_aggregation_scale_factor = 2.0
        self.sequence_len = 256
        self.db = False
        self.write_test_results_to = None
        self.update_all_params = False
        self.verbose = False

        # load input arguments
        self.__dict__.update((k, v) for k, v in kwargs.items() if k in params)

        self.model = BertForSequenceClassification.from_pretrained(self.bert_model)
        self.loss = CrossEntropyLoss(reduction='none')

        if not self.update_all_params:
            # tells the optimiser not to propagate the gradient over the entire model
            for name, param in self.model.named_parameters():
                if name.startswith('embeddings'):
                    param.requires_grad = False

        # this function is not in versions <0.8.1
        self.save_hyperparameters('epochs', 'lr', 'momentum')

        self.metrics = (M.Accuracy(), M.Precision(), M.Recall(), M.F1(), M.AUROC())
        self.metric_names = ('acc', 'prec', 'recall', 'f1', 'auroc')

    def setup(self, stage):

        def _dataframe_setup(nfp, rfp, split=True):
            if self.verbose:
                print('Reading data from .csv...')
            text_df = load_txt_df(
                fp=nfp,
                var=self.txtvar,
                st_aug=self.st_aug,
                _slice=2*self.batch_size if self.db else None
            )
            labelled_text = text_df.merge(read_csv(rfp, index_col=0), on='SUBJECT_ID', how='left')

            def _make_sample_weights(y):
                class_weight = torch.as_tensor(len(y)/(len(np.unique(y))*np.bincount(y)))
                return torch.tensor(
                    [class_weight[0].item() if sample == 0 else class_weight[1].item() for sample in y]
                )

            # add semantic type codes if specified
            if self.st_aug:
                labelled_text[self.txtvar] += labelled_text['SEMTYPES']
                labelled_text[self.txtvar] = labelled_text[self.txtvar].apply(str)
                labelled_text.drop('SEMTYPES', axis=1, inplace=True)

            if split:
                # do random split of patients list to ensure notes for the same patient don't get split between the training and validation sets
                all_patients = labelled_text.SUBJECT_ID.drop_duplicates().tolist()
                n_val_patients = int(len(all_patients)*self.val_frac)
                val_patients = sample(all_patients, n_val_patients)
                val_df = labelled_text[labelled_text.SUBJECT_ID.isin(val_patients)]
                train_df = labelled_text[~labelled_text.SUBJECT_ID.isin(val_df.SUBJECT_ID)]

                return train_df, val_df, _make_sample_weights(val_df.READM.values)
            else:
                return labelled_text, _make_sample_weights(labelled_text.READM.values)

        if stage == 'fit':
            if self.verbose:
                print('Loading training & validation datasets...')
            self.train_df, self.val_df, self.dev_sample_weight = _dataframe_setup(self.n_train_fp, self.r_train_fp)
        if stage == 'test':
            if self.verbose:
                print('Loading test dataset...')
            self.test_df, self.test_sample_weight = _dataframe_setup(self.n_test_fp, self.r_test_fp, split=False)

    def forward(self, input_ids, attn_masks):
        logits, = self.model(input_ids, attn_masks.float())

        return logits

    def train_dataloader(self):
        train_ds = EncodedDataset(self.train_df, self.bert_model, self.txtvar, self.sequence_len)
        return data.DataLoader(
            train_ds,
            batch_size=self.batch_size,
            sampler=data.RandomSampler(train_ds),
            num_workers=self.threads
        )

    def training_step(self, batch, batch_idx):
        logits = self.forward(batch['input_ids'], batch['attn_masks'])
        loss = self.loss(logits, batch['labels']).mean()

        return {'loss':loss, 'log':{'train_loss':float(loss)}}

    def val_dataloader(self):
        val_ds = EncodedDataset(self.val_df, self.bert_model, self.txtvar, self.sequence_len)
        return data.DataLoader(
            val_ds,
            batch_size=self.batch_size,
            sampler=data.SequentialSampler(val_ds),
            num_workers=self.threads
        )

    def validation_step(self, batch, batch_idx):
        logits = self.forward(batch['input_ids'], batch['attn_masks'])
        loss = self.loss(logits, batch['labels'])
        predictions = logits.argmax(-1)

        return {'loss':loss.detach(), 'predictions':predictions.detach(), 'labels':batch['labels'].detach()}

    def validation_epoch_end(self, outputs):
        loss, predictions, labels = map(lambda s: torch.cat([o[s] for o in outputs], 0), ('loss', 'predictions', 'labels'))
        if sum(labels).item() in [len(labels), 0.0]:
            raise RuntimeWarning('val labels all the same, skipping epoch_end step')
        else:
            out = {'loss':loss}
            out.update((name, metric(predictions, labels).item()) for name, metric in zip(self.metric_names, self.metrics))

        if self.write_dev_results_to is not None:
            with open(self.write_dev_results_to, 'w+') as dev_log:
                dev_log.write('-- DEV RESULTS --')
                for k, v in out.items():
                    dev_log.write(f'{k} : {v}')

        return {**out, 'log':out}

    def test_dataloader(self):
        test_ds = EncodedDataset(self.test_df, self.bert_model, self.txtvar, self.sequence_len)
        return data.DataLoader(
            test_ds,
            batch_size=self.batch_size,
            sampler=data.SequentialSampler(test_ds),
            num_workers=self.threads
        )

    def test_step(self, batch, batch_idx):
        logits = self.forward(batch['input_ids'], batch['attn_masks'])
        loss = self.loss(logits, batch['labels'])

        return {
            'loss':loss.detach(),
            'logits':logits.detach(),
            'labels':batch['labels'].detach(),
            'log':{'test_loss':loss}
        }

    def test_epoch_end(self, outputs):
        loss = torch.cat([o['loss'] for o in outputs]).mean()
        logits, labels = self._aggreg_subseq_logits(outputs)
        out = {'loss':float(loss)}
        predictions = logits.argmax(-1)
        labels.cuda()
        predictions.cuda()
        out.update((name, metric(predictions, labels).item()) for name, metric in zip(self.metric_names, self.metrics))

        if self.write_test_results_to is not None:
            with open(self.write_test_results_to, 'w+') as test_log:
                test_log.write('-- TEST RESULTS --\n')
                for k, v in out.items():
                    test_log.write(f'{k} : {v}\n')

        return {**out, 'log':out}

    def configure_optimizers(self):
        if self.optimiser == 'sgd':
            optim = SGD(
                self.parameters(),
                lr=self.hparams.lr,
                momentum=self.hparams.momentum
            )
            sched = CyclicLR(
                optim,
                base_lr=1e-8,
                max_lr=self.hparams.lr
            )
            return [optim], [sched]
        elif self.optimiser == 'adam':
            return AdamW(
                self.parameters(),
                lr=self.lr
            )
        else:
            raise NameError('invalid string passed to optimiser argument')

    def _aggreg_subseq_logits(self, output_list):
        '''
        This aggregates across the estimated readmission probability for each of the
        subsequences associated with each patient and outputs a readmission probability
        for each patient along with the reduced list of labels with which to calculate
        the test metrics
        '''
        def _scale(logits):
            factor = len(logits)/self.proba_aggregation_scale_factor
            return (np.max(logits)+np.mean(logits)*factor)/(1+factor)

        logits, labels, patient_ids = map(
            lambda s: [o[s] for o in output_list],
            ('logits', 'labels', 'patient_ids')
        )

        patient_logit_map, label_list = {}, []
        for patient, label, i in zip(patient_ids, labels, range(logits.shape[0])):
            if patient not in patient_logit_map:
                patient_logit_map[patient] = logits[i,:]
                label_list.append(label)
            else:
                patient_logit_map[patient] = torch.stack((patient_logit_map[patient, logits[i,:]]))
        for j in range(logits.shape[1]):
            out_logit_list = []
            for _logits in patient_logit_map.values():
                try:
                    out_logit_list.append(_scale(_logits[:,j]))
                except IndexError:
                    out_logit_list.append(_logits[j])
                if 'output_logits' in locals():
                    output_logits = torch.cat(
                        (output_logits, torch.tensor(out_logit_list).reshape((len(out_logit_list), 1))), dim=1
                    )
                else:
                    output_logits = torch.tensor(out_logit_list).reshape((len(out_logit_list), 1))

        return output_logits, torch.tensor(label_list)
