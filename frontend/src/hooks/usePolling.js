import { useEffect } from 'react';

const usePolling = (callback, interval = 5000) => {
  useEffect(() => {
    callback();

    const timer = setInterval(() => {
      callback();
    }, interval);

    return () => clearInterval(timer);
  }, [callback, interval]);
};

export default usePolling;