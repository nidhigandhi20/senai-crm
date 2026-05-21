import { useEffect, useState } from 'react';
import api from '../services/api';

const useAnalytics = () => {
  const [trend, setTrend] = useState([]);
  const [categories, setCategories] = useState([]);

  useEffect(() => {
    fetchAnalytics();
  }, []);

  const fetchAnalytics = async () => {
    try {
      const trendRes = await api.get('/analytics/sentiment-trend');
      const catRes = await api.get('/analytics/category-breakdown');

      setTrend(trendRes.data.data_points || []);
      setCategories(catRes.data.categories || []);
    } catch (err) {
      console.error(err);
    }
  };

  return {
    trend,
    categories,
  };
};

export default useAnalytics;