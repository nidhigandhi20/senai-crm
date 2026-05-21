import { useEffect, useState } from 'react';
import api from '../services/api';

const useThreads = (email) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  const fetchThread = async () => {
    try {
      const res = await api.get(`/threads/${email}`);
      setData(res.data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (email) {
      fetchThread();
    }
  }, [email]);

  return {
    data,
    loading,
    refresh: fetchThread,
  };
};

export default useThreads;