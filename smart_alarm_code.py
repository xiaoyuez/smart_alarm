# -*- coding: utf-8 -*-
"""smart_alarm_code.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1ckC-0YBamxJnxkabmQvXLoiaoBPuQGxS
"""

# This is the final code for the Smart Alarm project
# It performs unsupervised anomaly detection on animal behavioural data 
# It is recommended to run this on Google colab with GPU on 
# Written and organised by Xiaoyue Zhu, Dec 2019

# import the libraries
# general 
import pdb 
import tqdm
import time
import datetime
from datetime import timedelta
import numpy as np
import pandas as pd
from scipy import stats

# plotting
import matplotlib.pyplot as plt
import seaborn as sns

# ARIMA
from statsmodels.tsa.arima_model import ARIMA

# RMSE
from sklearn.metrics import mean_squared_error
from math import sqrt

# PCA 
from sklearn.decomposition import PCA
from sklearn.preprocessing import RobustScaler

# LSTM
from keras.models import Sequential, Model, Input
from keras.layers import Dense, LSTM, Dropout, TimeDistributed, RepeatVector

# r packages
import rpy2
from rpy2.robjects.packages import importr
from rpy2.robjects import pandas2ri
pandas2ri.activate()
base = importr('base')
utils = importr("utils")
stats = importr('stats')

utils.install_packages('ForeCA')
foreca = importr('ForeCA')
utils.install_packages('whitening')
whitening = importr('whitening')

# define the hyperparamters
N_COMP = 6 # how many ForeCA components
W_WIDTH = 60 # rolling window prediction width
COMP_THRESHOLD = 2 # how many comp think this is an anomaly for it to be an anomaly

# define the RigAlarm class
# since I can't give you access to our database, do not use the get_sessdata function

class RigAlarm: 
    def __init__(self, subjid, n_comp = N_COMP, w_width = W_WIDTH, 
                 rolling = True, comp_threshold = 2, pred_index = -1, 
                 n_steps = 3, pct_change_threshold = 60, 
                 key_features = ['num_trials','total_profit','hits','BotCin','MidCin','BotLin','BotRin']): 
      self.subjid = subjid
      self.n_comp = n_comp # how many components to use in ForeCA
      self.w_width = w_width # window width when applying ARIMA
      self.rolling = True # rolling or expanding window prediction
      self.comp_threshold = comp_threshold # report anomaly only when >= comp_threshold think it is one 
      self.pred_index = pred_index # -1 means predict the last session, e.g. -3 means predict the third last session
      self.is_anomaly = False # default
      self.n_steps = n_steps # p term for ARIMA and n_steps for LSTM 

      # these two things can be adjusted according to experimenter needs 
      self.pct_change_threshold = pct_change_threshold # the pct change threshold of the key features to be qualified as an anomaly
      self.key_features = key_features # a list of protocol-relevant features used in the final step of anomaly evaluation

    def split_sequences(self, sequences, n_steps):
      # split multivariate time series into chunked sequences
      X, y = list(), list()
      for i in range(len(sequences)):
        # find the end of this pattern
        end_ix = i + n_steps
        # check if we are beyond the dataset
        if end_ix >= len(sequences):
          break
        # gather input and output parts of the pattern
        seq_x, seq_y = sequences[i:end_ix, :], sequences[end_ix, :]
        X.append(seq_x)
        y.append(seq_y)
      return np.array(X), np.array(y)
    
    #def get_dbe(self):
    #  # secret codes 
    #  dbe = 'secret connection'
    #  return(dbe)

    #def get_sessdata(self): 
    #  # prepare session date given the subjid, can only be used with dbe
    #  dbe = self.get_dbe()
    # sqlstr = 'select * from beh.sessview where subjid = {} order by sessiondate'.format(self.subjid)
    #  sessview_df = pd.read_sql(sqlstr,dbe) 
    #  sqlstr = 'select sessid, TopLin, TopRin, MidLin, MidCin, MidRin, BotLin, BotCin, BotRin from testing.sesspokes where subjid = {}'.format(self.subjid)
    #  pokes_df = pd.read_sql(sqlstr,dbe) 
    #  sess_df = pd.merge(sessview_df, pokes_df, on = 'sessid').sort_values(by='sessiondate').reset_index(drop=True)
    #  return sess_df
  
    def preprocess(self, sess_df):
      # preprocess sess_df to be foreCA ready
      # sess_df = self.get_sessdata()

      # drop the unwanted columns 
      clean_df = sess_df.drop(columns = ['sessid','rigid','subjid','protocol',
                                      'sessiondate', 'rig_starttime', 'start_time', 
                                      'startstage','end_time', 'end_stage', 
                                      'bias', 'stage', 'species',
                                      'expgroup','settings_name'],axis = 1)
      
      # handle the missing values
      clean_df['mass'] = clean_df['mass'].fillna(method = 'ffill')
      clean_df['mass'] = clean_df['mass'].fillna(value = clean_df['mass'].mean())
      clean_df = clean_df[clean_df['total_profit'].notna()]
      clean_df = clean_df[clean_df['sess_min']>50]
      clean_df = clean_df.fillna(value = 0) # replace all the NAs in pokes with 0
      clean_array = clean_df.to_numpy().astype('float32')
      self.clean_df = clean_df
      return clean_array

    def foreCA(self, clean_array):
      # apply foreCA on the preprocessed data
      # first we check if the matrix is full rank using R stats package
      lambdas = base.eigen(stats.cov(clean_array)).rx2('values')
      lambdas = np.round(np.array(lambdas),decimals = 10)
      is_zeros = []

      # if the matrix is not full rank
      while sum(lambdas == 0) >= 1: 
        # throw away columns that make the matrix not full rank
        not_zeros = np.where(np.abs(lambdas) > 0)[0]
        is_zeros = np.append(is_zeros, np.where(lambdas == 0)[0])
        clean_array = clean_array[:,not_zeros]
        lambdas = base.eigen(stats.cov(clean_array)).rx2('values')
        lambdas = np.round(np.array(lambdas),decimals = 10)
        if sum(lambdas == 0) == 0: 
          raise Warning('The original matrix was not full rank. Columns {} were discarded.'.format(is_zeros))
      
      # apply foreCA on the whitened data
      whitened_array = foreca.whiten(clean_array)[5]
      model = foreca.foreca(whitened_array, n_comp = self.n_comp)
      self.foreca_scores_ = np.array(model.rx2('scores'))
      self.foreca_loadings_ = np.array(model.rx2('loadings'))
      self.foreca_omegas_ = np.array(model.rx2('Omega'))
      return

    def arima_predict(self):
      # use ARIMA to predict the next value for each foreCA component 
      # the default of pred_index is -1: predict the last value
      # it can also be set to e.g. -3, predict the third last value
      self.y_pred_ = np.zeros(self.n_comp)
      self.y_low_ = np.zeros(self.n_comp)
      self.y_high_ = np.zeros(self.n_comp) 
      self.y_true_ = self.foreca_scores_[self.pred_index,:]

      # loop over each foreCA component 
      for comp in range(self.n_comp): 
        if self.rolling == True: # if doing rolling window prediction
          training_scores = self.foreca_scores_[self.pred_index - self.w_width : self.pred_index, comp] # leave out today's score
        else: # if doing expanding window prediction
          training_scores = self.foreca_scores_[:self.pred_index, comp]

        # apply ARIMA 
        arima = ARIMA(training_scores, order = (self.n_steps,1,1))
        try:
          arima_fit = arima.fit(disp = False, 
                                tol = 1e-05,
                                method = 'mle',
                                solver = 'bfgs')
          self.y_pred_[comp], _, y_conf = arima_fit.forecast(alpha = 0.1)
          self.y_low_[comp] = y_conf[0][0]
          self.y_high_[comp] = y_conf[0][1]
        except Exception: # when ARIMA failed to converge for some reason
          #raise Warning('ARIMA failed to converge for today''s foreCA component {}'.format(comp+1))           
          pass
      
      # just in case, remove NAs in the prediction
      self.y_pred_[np.isnan(self.y_pred_)] = 0
      self.y_low_[np.isnan(self.y_low_)] = 0
      self.y_high_[np.isnan(self.y_high_)] = 0
      pass
    
    def lstm_predict(self):
      # use LSTM to predict the next value for all the foreCA components
      # this is not used in the main solution, it is rather for comparison and plotting
      inp = Input(shape = (self.n_steps, self.n_comp)) #n_steps, n_features
      x = LSTM(64, activation='relu')(inp)
      x = Dropout(0.5)(x, training = True) # apply dropout to approximate confidence intervals
      out = Dense(6)(x)
      lstm = Model(inputs = inp, outputs = out)
      lstm.compile(loss = 'mae', optimizer = 'adam')

      # prepare training set
      if self.rolling == True: 
        training_scores = self.foreca_scores_[self.pred_index - self.w_width : self.pred_index, :]
      else: 
        training_scores = self.foreca_scores_[:self.pred_index, :] 

      # split a multivariate sequence into samples
      X, y = self.split_sequences(training_scores, self.n_steps)

      # train LSTM
      self.lstm_history = lstm.fit(X, y, epochs = 50, batch_size = 64, verbose = 0, shuffle = False)

      # prepare test set 
      X_test = training_scores[-self.n_steps:,:]
      X_test = np.reshape(X_test, (1, self.n_steps, self.n_comp))

      # Get LSTM confidence interval using Monte Carlo sampling methods
      mc_pred = []
      mc_times = 50 # how many times of sampling?
      for i in range(mc_times):
        out = lstm.predict(X_test)
        mc_pred.append(out)
      mc_pred = np.array(mc_pred)

      mean_pred = np.array(mc_pred).mean(axis = 0)
      conf_high = mean_pred + 1.645 * (np.array(mc_pred).std(axis = 0) / np.sqrt(mc_times)) # 90% confidence interval
      conf_low = mean_pred - 1.645 * (np.array(mc_pred).std(axis = 0) / np.sqrt(mc_times)) # 90% confidence interval

      # store the prediction results 
      self.y_true_ = self.foreca_scores_[self.pred_index,:]
      self.lstm_pred_ = mean_pred
      self.lstm_y_low_ = conf_low
      self.lstm_y_high_ = conf_high
      return

    def detect_outliers(self):   
      # detect outliers based on ARIMA confidence intervals 
      self.outlier_comp = []
      self.abs_conf_diff_ = []

      # loop over each valid foreCA component to see where it lies with respect to ARIMA confidence interval
      for comp in range(self.n_comp): 
        if self.y_pred_[comp] == 0: # if this component is not converged, skip it 
          continue
        else:
          if (self.y_true_[comp] < self.y_low_[comp]) or (self.y_true_[comp] > self.y_high_[comp]): # if is an outlier 
            self.outlier_comp = np.append(self.outlier_comp, comp + 1)  
            if (self.y_true_[comp] < self.y_low_[comp]): # if lower than conf
              acd = np.abs(self.y_low_[comp] - self.y_true_[comp]) 
            else: # if higher than conf
              acd = np.abs(self.y_true_[comp] - self.y_high_[comp])
            self.abs_conf_diff_ = np.append(self.abs_conf_diff_, acd)

      # determine if this session is an anomaly 
      if len(self.outlier_comp) > self.comp_threshold:
        self.is_anomaly = True

        # retrieve the original session data and compare it to the previous session
        if self.pred_index == -1: # indexing issues
          compare_df = self.clean_df.iloc[self.pred_index-1:]
        else: 
          compare_df = self.clean_df.iloc[self.pred_index-1:self.pred_index+1]
        self.compare_df = compare_df

        # quantify the change 
        cols = ['num_trials','hits','viols','total_profit','mass',
               'TopLin', 'TopRin', 'MidLin', 'MidCin', 'MidRin', 'BotLin', 'BotCin', 'BotRin']
        diff_pct = np.divide((compare_df.iloc[1].loc[cols] - compare_df.iloc[0].loc[cols]), compare_df.iloc[0].loc[cols])
        self.diff_df = pd.DataFrame(diff_pct * 100)

        # if there is less than pct_change_threshold in the key features columns 
        if np.max(np.abs(self.diff_df.loc[self.key_features]))[0] < self.pct_change_threshold:
          self.is_anomaly = False # maybe the algorithm's criterion is not the same as yours 

        return 

    def run(self, sess_df):
      clean_array = self.preprocess(sess_df)
      self.foreCA(clean_array)
      self.arima_predict()
      self.detect_outliers()
      print("The anomaly status of subject {} on {} is {} with {} outlier components.".format(
          self.subjid, sess_df.sessiondate.iloc[self.pred_index], 
          self.is_anomaly, len(self.outlier_comp)))
      return

# you can try it out using the example data
# it is kinda slow bc every time you run it, it has to compute ForeCA
# in practice, ForeCA does not need to be computed every day 
df = pd.read_csv('example_sessdata0.csv') 
subj_list = np.unique(df.subjid)

pred_index = -6 # the session you want to check if it's an anomaly, -1 means the last session, -3 means the third last etc.
for subj in subj_list:
  sess_df = df[df.subjid == subj]
  ra = RigAlarm(subj, pred_index = pred_index)
  ra.run(sess_df)

# Compute root mean squared error (RMSE) and store the prediction results using rolling window predictions
# given the subject list and method (ARIMA or LSTM)

def rolling_pred(subj_list, method = 'arima'):
  rmse = np.zeros((len(subj_list), N_COMP))
  for i, subj in enumerate(subj_list):
    y_true, y_pred = [], []
    conf_low, conf_high = [], []

    # initialise RigAlarm class for each subject
    ra = RigAlarm(subj)
    sess_df = df[df.subjid == subj]
    if sess_df.shape[0] < ra.w_width: # just in case this subject has too little data
      continue
    sess_array = ra.preprocess(sess_df)
    ra.foreCA(sess_array)

    # now we iterate over every session, going backwards
    for j in tqdm.tqdm(range(sess_array.shape[0])):
      if j > (sess_array.shape[0] - W_WIDTH): # stop the loop when the w_width session is reached
        break
      else:
        ra.pred_index = -(j+1) # start from the last day
        if method == 'arima':
          ra.arima_predict()
          y_true.append(ra.y_true_)
          y_pred.append(ra.y_pred_)
          conf_low.append(ra.y_low_)
          conf_high.append(ra.y_high_)
        elif method == 'lstm':
          ra.lstm_predict()
          y_true.append(ra.y_true_)
          y_pred.append(ra.lstm_pred_)
          conf_low.append(ra.lstm_y_low_)
          conf_high.append(ra.lstm_y_high_)

  # reverse the order bc we went backwards
  y_true = np.array(y_true)[::-1]
  y_pred = np.array(y_pred)[::-1]
  conf_low = np.array(conf_low)[::-1]
  conf_high = np.array(conf_high)[::-1]

  # compute RMSE of all foreCA components     
  for comp in range(N_COMP):
    rmse[i,comp] = sqrt(mean_squared_error(y_true[:,comp], y_pred[:,comp]))
  
  # get the population RMSE stats
  rmse_mean = np.mean(rmse, axis=0)
  rmse_std = np.std(rmse, axis=0)

  return rmse_mean, rmse_std, y_true, y_pred, conf_low, conf_high

subj_list = [2077] # just use one subject to save time 
# Get RMSE stats and prediction results for plotting 
arima_rmse_mean, arima_rmse_std, y_true, arima_y_pred, arima_conf_low, arima_conf_high = rolling_pred(subj_list, method = 'arima') # 10m or so with GPU
#lstm_rmse_mean, lstm_rmse_std, y_true, lstm_y_pred, lstm_conf_low, lstm_conf_high = rolling_pred(subj_list, method = 'lstm') # 30m - 1h, do not recommend running

## PlOTTING FUNCTIONS
# define a customised plot format
def label_plot(ax, xlabel_name, ylabel_name):
  ax.tick_params(axis = 'both', which = 'major', labelsize = 40)
  ax.set_xlabel(xlabel_name, fontdict = {'fontsize':50})
  ax.set_ylabel(ylabel_name, fontdict = {'fontsize':50})
  a = ax.get_yticks()
  ax.set_yticks([a[0],a[-1]])
  return ax

# feature distribution plot
def plot_feature_dist(subjid, feature):
  sess_df = df[df.subjid == subjid]
  ra = RigAlarm(subjid)
  ra.preprocess(sess_df)
  clean_df = ra.clean_df
  #fig = plt.figure(figsize=(15,7))
  fig = sns.distplot(clean_df[feature])
  ax = plt.gca()
  label_plot(ax, feature," ")
  return fig, ax

# PCA / ForeCA plots
def plot_dim_reducers(subjid, method = 'pca', which_comp = 0):
  ra = RigAlarm(subjid)
  sess_df = df[df.subjid == subjid]
  clean_array = ra.preprocess(sess_df)
  clean_df = ra.clean_df
  if method == 'pca':
    cl = 'gray'
    # first we normalise data using RobustScaler, it is robust to outliers
    rbs = RobustScaler()
    clean_scaled = rbs.fit_transform(clean_df)
    # apply PCA
    pca = PCA(n_components = N_COMP)
    scores = pca.fit_transform(clean_scaled)
    x_captured = pca.explained_variance_ratio_
    # get relative loadings / feature importance
    loadings = pca.components_.transpose()
    relative_loadings = np.divide(loadings,np.std(loadings,axis = 0))
  elif method == 'foreca':
    # apply foreca using the function written in RigAlarm class
    cl = 'dodgerblue'
    ra.foreCA(clean_array)
    x_captured= ra.foreca_omegas_
    scores = ra.foreca_scores_
    loadings = ra.foreca_loadings_
    relative_loadings = np.divide(loadings,np.std(loadings,axis = 0))
  
  # variance / omega explained plot
  fig1 = plt.figure(figsize = (15,7))
  sns.barplot(list(range(1, N_COMP + 1)), x_captured, color = cl)
  ax = plt.gca()
  label_plot(ax, "PCA components",'% Var explained')

  # component projection plot
  fig2 = plt.figure(figsize = (15,7))
  plt.plot(scores[:,which_comp], color = cl, linewidth = 5)
  ax = plt.gca()
  label_plot(ax, "sessions", "Component value")

  # feature importance plot
  fig3 = plt.figure(figsize = (15,7))
  n_columns = clean_df.shape[1]
  sns.barplot(list(range(n_columns)), relative_loadings[:,which_comp], color = cl)
  plt.xticks(ticks = list(range(n_columns)), labels = clean_df.columns, fontsize = 20, rotation = 80)
  ax = plt.gca()
  label_plot(ax, " ","Feature importance")

  # the main feature plot
  fig4 = plt.figure(figsize = (15,7))
  main_feature = clean_df.columns[np.argmax(np.abs(relative_loadings[:,0]))]
  plt.plot(clean_df[main_feature], color = cl, linewidth = 5)
  ax = plt.gca()
  label_plot(ax, "sessions", main_feature)

  return fig1, fig2, fig3, fig4 

# find outliers given a component
def find_outliers(y_true, conf_low, conf_high, comp=0):
  y_true_c = y_true[:,comp]
  conf_low_c = conf_low[:,comp]
  conf_high_c = conf_high[:,comp]

  low_outliers_x = np.where(y_true_c < conf_low_c)
  high_outliers_x = np.where(y_true_c > conf_high_c)
  outliers_x = np.append(low_outliers_x, high_outliers_x)
  outliers_y = y_true_c[outliers_x]
  return outliers_x, outliers_y 

# find anomalies combining the votes from all components
def find_anomalies(y_true, conf_low, conf_high, comp_threshold=COMP_THRESHOLD):
  outliers_x = np.empty([1])
  for i in range(N_COMP):
    ox, oy = find_outliers(y_true, conf_low, conf_high, i)
    outliers_x = np.append(outliers_x, ox)
  df = pd.DataFrame(outliers_x, columns = ['outliers_x'])
  anomalies_x = df.outliers_x.value_counts()[df.outliers_x.value_counts() > comp_threshold]
  return np.array(anomalies_x.index).astype(int) # return the index of anomalies 

# plot rolling prediction results and detected anomalies
def plot_rolling_pred(method = 'arima', which_comp = 0, plot_outlier = True, plot_anomaly = False):
  if method == 'arima':
    y_pred = arima_y_pred
    conf_low = arima_conf_low
    conf_high = arima_conf_high
  elif method == 'lstm':
    y_pred = lstm_y_pred
    conf_low = lstm_conf_low
    conf_high = lstm_conf_high
  
  x = range(W_WIDTH, y_true.shape[0] + W_WIDTH)
  fig = plt.figure(figsize = (15, 7))
  plt.plot(x, y_true[:,which_comp], color = 'gray', label='True')
  plt.plot(x, y_pred[:,which_comp], color = 'salmon', label='Pred')
  plt.fill_between(x, conf_low[:,which_comp], conf_high[:,which_comp], color = 'salmon', alpha =0.3, label = '90% Conf Interval')

  # if we need to plot the outliers
  if plot_outlier:
    outliers_x, outliers_y = find_outliers(y_true, conf_low, conf_high, comp = which_comp)
    plt.scatter(outliers_x + W_WIDTH, outliers_y, 
              facecolor = (1, 1, 0, 0), edgecolors = 'dodgerblue', s = 70, linewidth = 2, label = 'Detected Outliers')
  # if we need to plot the anomalies 
  elif plot_anomaly:
    anomalies_x = find_anomalies(y_true, conf_low, conf_high)
    anomalies_y = y_true[anomalies_x, which_comp]
    plt.scatter(anomalies_x + W_WIDTH, anomalies_y, 
              facecolor = (1, 1, 0, 0), edgecolors = 'dodgerblue', s = 70, linewidth = 2, label = 'Detected Anomalies')
    
  plt.legend(fontsize = 20)
  plt.title('Component {}'.format(which_comp + 1), fontdict = {'fontsize': 45})
  ax = plt.gca()
  label_plot(ax, "sessions", "Component value")
  return fig, ax

# feature distribution plots
plot_feature_dist(2077,'num_trials') # change the feature name to anything you want to plot

# PCA / ForeCA diagnostic plots
plot_dim_reducers(2077, method='pca', which_comp=0)
plot_dim_reducers(2077, method='foreca', which_comp=0)

# first we just plot arima prediction
plot_rolling_pred(method='arima', plot_outlier=False, plot_anomaly=False)
# then we plot arima prediction and detected outliers on this component
plot_rolling_pred(method='arima', plot_outlier=True, plot_anomaly=False)
# finally we plot arima prediction and detected anomalies 
plot_rolling_pred(method='arima', plot_outlier=False, plot_anomaly=True)